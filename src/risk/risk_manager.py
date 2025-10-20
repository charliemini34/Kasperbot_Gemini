# Fichier: src/risk/risk_manager.py
# Version: 18.1.6 (Fix-Decimal-Float-TypeError)
# Dépendances: MetaTrader5, pandas, numpy, logging, decimal, pytz, datetime
# Description: Correction TypeError (Decimal+float) dans is_daily_loss_limit_reached.

import MetaTrader5 as mt5
import pandas as pd
import numpy as np
import logging
from decimal import Decimal, ROUND_HALF_UP, InvalidOperation
from datetime import datetime, time as dt_time, timedelta
import pytz

class RiskManager:
    """
    Gère tous les aspects du risque: calcul de taille de position,
    gestion des stops (SL, BE, Trailing), et limites de perte.
    """

    def __init__(self, config: dict, executor, symbol: str):
        self.config = config
        self.executor = executor # MT5Executor instance
        self.mt5 = executor.mt5 # Accès direct à l'instance MT5 connectée
        self.symbol = symbol

        # Paramètres de risque globaux
        risk_settings = self.config.get('risk_management', {})
        self.risk_per_trade_pct = Decimal(str(risk_settings.get('risk_per_trade', 1.0))) / Decimal('100.0')
        self.daily_loss_limit_pct = Decimal(str(risk_settings.get('daily_loss_limit_percent', 5.0))) / Decimal('100.0')
        self.max_concurrent_risk_pct = Decimal(str(risk_settings.get('max_concurrent_risk_percent', 3.0))) / Decimal('100.0')

        # Paramètres de gestion de trade
        management_settings = self.config.get('trade_management', {})
        self.ptp_rules = management_settings.get('partial_take_profit', [])
        self.breakeven_rules = management_settings.get('breakeven', {})
        self.trailing_stop_rules = management_settings.get('trailing_stop_atr', {})

        # Paramètres de SL/TP (stratégie et fallbacks)
        sl_tp_settings = self.config.get('sl_tp_strategy', {})
        self.sl_strategy = sl_tp_settings.get('strategy', 'ATR_MULTIPLE')
        self.tp_strategy = sl_tp_settings.get('tp_strategy', 'ATR_MULTIPLE')
        self.sl_atr_multiplier = Decimal(str(sl_tp_settings.get('sl_atr_multiple_default', 2.0)))
        self.tp_atr_multiplier = Decimal(str(sl_tp_settings.get('tp_atr_multiple_default', 3.0)))
        self.sl_buffer_pips = Decimal(str(sl_tp_settings.get('sl_buffer_pips', 1.0)))
        self.tp_buffer_pips = Decimal(str(sl_tp_settings.get('tp_buffer_pips', 0.0)))

        # Paramètres spécifiques au symbole (override)
        symbol_override = self.config.get('symbol_specific_settings', {}).get(symbol, {})
        if 'sl_atr_multiple' in symbol_override: self.sl_atr_multiplier = Decimal(str(symbol_override['sl_atr_multiple']))
        if 'tp_atr_multiple' in symbol_override: self.tp_atr_multiplier = Decimal(str(symbol_override['tp_atr_multiple']))
        # (Ajouter d'autres overrides si nécessaire, ex: risk_per_trade)

        # Infos Symbole MT5 (critique)
        self.symbol_info = self.mt5.symbol_info(self.symbol)
        if not self.symbol_info:
            logging.error(f"Impossible d'obtenir les infos pour {self.symbol}. RiskManager inutilisable pour ce symbole.")
            raise ValueError(f"Infos symbole MT5 introuvables pour {self.symbol}")

        self.digits = self.symbol_info.digits
        self.point = self.symbol_info.point
        self.volume_min = self.symbol_info.volume_min
        self.volume_max = self.symbol_info.volume_max
        self.volume_step = self.symbol_info.volume_step
        self.trade_contract_size = self.symbol_info.trade_contract_size
        self.currency_profit = self.symbol_info.currency_profit # Devise de profit (ex: USD pour EURUSD)
        self.currency_margin = self.symbol_info.currency_margin # Devise de marge (ex: EUR pour EURUSD)

        # Infos Compte MT5
        self.account_info = self.executor.get_account_info()
        self.account_currency = self.account_info.currency if self.account_info else "USD"


    def _calculate_atr(self, ohlc_data: pd.DataFrame, period: int = 14) -> float:
        """Calcule l'ATR (Average True Range)."""
        if ohlc_data is None or len(ohlc_data) < period:
            logging.warning(f"Données OHLC insuffisantes pour ATR({period}) sur {self.symbol}")
            return 0.0
        
        high_low = ohlc_data['high'] - ohlc_data['low']
        high_close = np.abs(ohlc_data['high'] - ohlc_data['close'].shift())
        low_close = np.abs(ohlc_data['low'] - ohlc_data['close'].shift())
        
        tr = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
        atr = tr.rolling(window=period).mean().iloc[-1]
        
        if pd.isna(atr) or atr == 0.0:
             logging.warning(f"Calcul ATR invalide (NaN ou 0.0) pour {self.symbol}. Utilisation fallback 10 pips.")
             return self.point * 10 # Fallback 10 pips
        
        return atr


    def get_conversion_rate(self, from_currency: str, to_currency: str) -> Decimal:
        """
        Obtient le taux de conversion entre deux devises via MT5.
        Ex: from_currency=EUR, to_currency=USD -> Ticker EURUSD
        Ex: from_currency=USD, to_currency=JPY -> Ticker USDJPY
        Ex: from_currency=CHF, to_currency=USD -> Ticker CHFUSD (si existe) ou 1/USDCHF
        """
        if from_currency == to_currency:
            return Decimal('1.0')

        # 1. Essayer le ticker direct (ex: EURUSD)
        ticker_direct = f"{from_currency}{to_currency}"
        tick_direct = self.mt5.symbol_info_tick(ticker_direct)
        if tick_direct and tick_direct.ask > 0:
            logging.debug(f"Taux conversion (direct) {ticker_direct} @ {tick_direct.ask}")
            return Decimal(str(tick_direct.ask)) # Utiliser Ask pour conversion (coût)

        # 2. Essayer le ticker inversé (ex: USDCHF pour CHFUSD)
        ticker_inverse = f"{to_currency}{from_currency}"
        tick_inverse = self.mt5.symbol_info_tick(ticker_inverse)
        if tick_inverse and tick_inverse.bid > 0: # Utiliser Bid pour l'inverse
            try:
                rate = Decimal('1.0') / Decimal(str(tick_inverse.bid))
                logging.debug(f"Taux conversion (inverse) {ticker_inverse} @ {tick_inverse.bid} -> 1/{tick_inverse.bid} = {rate}")
                return rate
            except (InvalidOperation, ZeroDivisionError):
                 logging.error(f"Division par zéro/Invalide pour taux inverse {ticker_inverse} (Bid={tick_inverse.bid})")
                 pass # Tenter le croisement

        # 3. Essayer via un croisement (ex: AUDCHF -> AUDUSD * USDCHF)
        # Simplification: Pour la plupart des brokers, le croisement majeur (USD) est disponible.
        if from_currency != "USD" and to_currency != "USD":
            try:
                rate_from_usd = self.get_conversion_rate(from_currency, "USD") # ex: AUDUSD
                rate_usd_to = self.get_conversion_rate("USD", to_currency)     # ex: USDCHF
                if rate_from_usd > 0 and rate_usd_to > 0:
                    crossed_rate = rate_from_usd * rate_usd_to
                    logging.debug(f"Taux conversion (croisé) {from_currency}USD * USD{to_currency} = {rate_from_usd} * {rate_usd_to} = {crossed_rate}")
                    return crossed_rate
            except Exception as e:
                logging.error(f"Erreur calcul taux croisé pour {from_currency}{to_currency}: {e}")

        logging.error(f"Impossible d'obtenir le taux de conversion de {from_currency} à {to_currency}.")
        return Decimal('0.0') # Signal d'erreur


    def _calculate_pip_value_in_account_currency(self, volume: Decimal = Decimal('1.0')) -> Decimal:
        """
        Calcule la valeur d'un pip pour 1 lot (ou volume spécifié)
        dans la devise du compte.
        """
        
        # 1. Devise de profit du symbole (ex: USD pour EURUSD)
        profit_currency = self.currency_profit
        
        # 2. Valeur du pip dans la devise de profit (formule standard)
        # Pour FX: (Point * ContractSize) / Taux_Quote_Profit
        # Pour Indices/etc: Souvent (Point * ContractSize) ou juste (TickSize * TickValue)
        
        pip_value_profit_currency = Decimal('0.0')

        if self.symbol_info.trade_calc_mode == mt5.SYMBOL_CALC_MODE_FX:
            # Cas standard FX (ex: EURUSD)
            # Valeur Pip = Point (ex: 0.00001) * ContractSize (ex: 100000) = 1 USD (si USD est devise de cotation)
            
            # Point est la taille du pip (ex: 0.0001 pour EURUSD 4 digits, 0.00001 pour 5 digits)
            # MAIS self.point est la *plus petite variation* (ex: 0.00001)
            # Si le symbole a 5 digits, 1 pip = 10 points (généralement)
            # Assumons que "pip" ici signifie la plus petite variation (point)
            
            pip_value_profit_currency = Decimal(str(self.point)) * Decimal(str(self.trade_contract_size))
            
            # Si la devise de cotation (profit) n'est pas la devise de base (margin)
            # ex: USDJPY. Profit en JPY. Point=0.001, CS=100000. PipValue = 100 JPY.
            # ex: EURUSD. Profit en USD. Point=0.00001, CS=100000. PipValue = 1 USD.
            
            # Cas spécifique où la devise de profit n'est pas la devise de cotation
            # (rare, mais vérifions le mode de calcul)
            # La plupart du temps, pip_value = (Point * ContractSize) [en devise de cotation]
            
            # Ajustement si la devise de cotation n'est pas la devise de profit ?
            # Pour EURUSD, profit_currency=USD. PipValue (1pt) = 0.00001 * 100000 = 1 USD.
            # Pour USDJPY, profit_currency=JPY. PipValue (1pt) = 0.001 * 100000 = 100 JPY.
            # Pour XAUUSD, profit_currency=USD. PipValue (1pt) = 0.01 * 100 = 1 USD.
            
            # Cas où la devise de profit est la devise de base (ex: indices)
            # (Non, c'est géré par d'autres modes)

            # Il faut diviser par le taux si la devise de profit est la devise de cotation
            # et que ce n'est pas USD (ex: EURCHF, profit en CHF)
            
            # Utilisons l'outil MT5 pour la valeur d'1 pip (1.0 lot)
            # mt5.symbol_info(symbol).trade_tick_value
            # trade_tick_value = valeur d'un tick (point) pour 1 lot
            
            if self.symbol_info.trade_tick_value > 0:
                 # C'est la valeur (en devise de marge) d'un tick (point) pour 1 lot
                 # Non, la doc MT5 dit: "Tick value for a tick size specified in trade_tick_size"
                 # C'est confus.
                 
                 # Recalculons manuellement la valeur d'un POINT (pas un pip)
                 
                 pip_value_profit_currency = Decimal(str(self.point * self.trade_contract_size))
                 
                 # Si la devise de profit (cotation) n'est pas USD (ex: USDJPY -> JPY)
                 # nous devons diviser par le taux de change actuel pour l'obtenir en USD (ou autre)
                 # Mais pip_value_profit_currency est déjà dans la bonne devise (JPY)
                 
                 # Pour EURUSD (profit=USD), 0.00001 * 100000 = 1 USD
                 # Pour USDJPY (profit=JPY), 0.001 * 100000 = 100 JPY
                 # Pour XAUUSD (profit=USD), 0.01 * 100 = 1 USD
                 
                 # Semble correct.
                 pass
                 
        elif self.symbol_info.trade_calc_mode in [mt5.SYMBOL_CALC_MODE_CFD, mt5.SYMBOL_CALC_MODE_CFDINDEX, mt5.SYMBOL_CALC_MODE_FUTURES]:
             # Indices, Matières Premières, etc.
             # La valeur du point est souvent fixe ou (point * contract_size)
             # Utilisons tick_value et tick_size (plus fiable)
             # 1 tick = trade_tick_size (ex: 0.01 pour XAUUSD)
             # 1 tick vaut trade_tick_value (ex: 0.01 USD pour XAUUSD)
             # 1 point (self.point) = (ex: 0.01 pour XAUUSD)
             
             if self.symbol_info.trade_tick_value > 0 and self.symbol_info.trade_tick_size > 0:
                 # Valeur d'un point = (point / tick_size) * tick_value
                 pip_value_profit_currency = (Decimal(str(self.point)) / Decimal(str(self.symbol_info.trade_tick_size))) * Decimal(str(self.symbol_info.trade_tick_value))
             else:
                 # Fallback si tick_value/tick_size non fiables
                 pip_value_profit_currency = Decimal(str(self.point * self.trade_contract_size))

        else: # Autres modes (non gérés)
             logging.warning(f"Mode calcul {self.symbol_info.trade_calc_mode} non géré pour {self.symbol}. Pip value peut être incorrecte.")
             pip_value_profit_currency = Decimal(str(self.point * self.trade_contract_size)) # Fallback


        # 3. Appliquer le volume (1.0 lot par défaut)
        pip_value_profit_currency *= volume

        # 4. Convertir dans la devise du compte (si nécessaire)
        if profit_currency == self.account_currency:
            return pip_value_profit_currency # Pas de conversion
        else:
            conversion_rate = self.get_conversion_rate(profit_currency, self.account_currency)
            if conversion_rate > 0:
                return pip_value_profit_currency * conversion_rate
            else:
                logging.error(f"Pip Value: Taux conversion {profit_currency}->{self.account_currency} invalide.")
                return Decimal('0.0') # Erreur


    def _calculate_volume(self, equity: float, sl_price: float, entry_price: float, direction: str) -> Decimal:
        """
        Calcule la taille de position (volume) basée sur le risque,
        l'equity, et la distance du Stop Loss.
        """
        
        # --- Début de la journalisation de trace pour validation démo ---
        log_entries = [f"Calcul Volume {self.symbol}:"]
        
        try:
            equity_decimal = Decimal(str(equity))
            log_entries.append(f"  1. Equity: {equity_decimal:.2f} {self.account_currency}")

            # 1. Montant à risquer (en devise du compte)
            risk_amount_account_currency = equity_decimal * self.risk_per_trade_pct
            log_entries.append(f"  2. Risque Config: {self.risk_per_trade_pct * 100:.2f}%")
            log_entries.append(f"  3. Montant Risqué: {risk_amount_account_currency:.2f} {self.account_currency}")

            # 2. Distance du SL (en points)
            sl_price_decimal = Decimal(str(sl_price))
            entry_price_decimal = Decimal(str(entry_price))
            
            if direction == "BUY":
                sl_distance_points = entry_price_decimal - sl_price_decimal
            else: # SELL
                sl_distance_points = sl_price_decimal - entry_price_decimal

            if sl_distance_points <= 0:
                logging.warning(f"Distance SL invalide (<= 0): {sl_distance_points}. Entrée: {entry_price}, SL: {sl_price}. Trade annulé.")
                log_entries.append(f"  ERREUR: Distance SL <= 0 ({sl_distance_points}).")
                logging.info("\n".join(log_entries))
                return Decimal('0.0')

            log_entries.append(f"  4. Entrée={entry_price_decimal}, SL={sl_price_decimal}")
            log_entries.append(f"  5. Distance SL (Points): {sl_distance_points}")
            
            # Convertir la distance en "pips" (si 10 points = 1 pip)
            # Pour le calcul, nous utilisons la distance en points (ex: 0.00150)

            # 3. Valeur d'un pip (point) pour 1 Lot (dans la devise du compte)
            # Note: _calculate_pip_value_in_account_currency utilise 'point'
            point_value_1_lot = self._calculate_pip_value_in_account_currency(Decimal('1.0'))
            
            if point_value_1_lot <= 0:
                 logging.error(f"Valeur du point (1 lot) invalide: {point_value_1_lot}. Impossible de calculer le volume.")
                 log_entries.append(f"  ERREUR: Valeur Point (1 lot) invalide: {point_value_1_lot} (Devise Profit: {self.currency_profit})")
                 logging.info("\n".join(log_entries))
                 return Decimal('0.0')
                 
            log_entries.append(f"  6. Valeur Point (1 Lot): {point_value_1_lot:.4f} {self.account_currency} (via {self.currency_profit})")

            # 4. Perte pour 1 Lot (en devise du compte)
            # (Distance en points * Valeur d'un point)
            # Ex: SL = 150 points (0.00150). Valeur point = 1 USD. Perte = 150 USD.
            # Ex: SL = 15.0 points (JPY). Valeur point = 100 JPY. Perte = 1500 JPY.
            
            # sl_distance_points (ex: 0.00150) / self.point (ex: 0.00001) = 150
            sl_distance_in_min_increments = sl_distance_points / Decimal(str(self.point))
            loss_per_lot = sl_distance_in_min_increments * point_value_1_lot
            
            log_entries.append(f"  7. Distance (Increments Min): {sl_distance_in_min_increments:.1f} (Points/{self.point})")
            log_entries.append(f"  8. Perte pour 1 Lot: {loss_per_lot:.2f} {self.account_currency}")

            if loss_per_lot <= 0:
                logging.error(f"Perte par lot invalide ({loss_per_lot}). SL trop proche ou erreur valeur pip.")
                log_entries.append(f"  ERREUR: Perte par lot invalide: {loss_per_lot}")
                logging.info("\n".join(log_entries))
                return Decimal('0.0')

            # 5. Calcul du volume
            # Volume = Montant à risquer / Perte pour 1 Lot
            volume = risk_amount_account_currency / loss_per_lot
            log_entries.append(f"  9. Volume (Brut): {volume:.8f} lots")

            # 6. Validation et Arrondi (Step)
            # Arrondir AU PLUS PROCHE, mais on pourrait arrondir en DESSOUS (floor) par prudence
            step = Decimal(str(self.volume_step))
            
            # Utiliser quantize pour arrondir au step le plus proche
            volume_rounded = (volume / step).quantize(Decimal('1.0'), rounding=ROUND_HALF_UP) * step
            # Par prudence (ne jamais dépasser le risque), préférer arrondir en dessous (FLOOR)
            # volume_rounded = (volume // step) * step 
            # -> Utilisons ROUND_HALF_UP comme implémenté (v18.1.2), mais loggons la différence.
            
            log_entries.append(f"  10. Volume (Arrondi à {step}): {volume_rounded} lots")


            # 7. Vérifier Limites Min/Max
            if volume_rounded < Decimal(str(self.volume_min)):
                logging.warning(f"Volume calculé {volume_rounded} < Min {self.volume_min}. Ajusté à Min (ou 0 si risque trop élevé).")
                # Si le volume min dépasse le risque, ne pas trader
                # (Non implémenté, on ajuste au min, augmentant le risque)
                # Ajoutons un check:
                volume_min_decimal = Decimal(str(self.volume_min))
                loss_at_min_volume = (volume_min_decimal / Decimal('1.0')) * loss_per_lot
                if loss_at_min_volume > (risk_amount_account_currency * Decimal('1.5')): # Tolérance 50%
                     logging.critical(f"RISQUE ÉLEVÉ: Volume min {self.volume_min} entraîne risque de {loss_at_min_volume:.2f} {self.account_currency} (limite {risk_amount_account_currency:.2f}). Trade annulé.")
                     log_entries.append(f"  ERREUR: Vol Min {self.volume_min} = Risque {loss_at_min_volume:.2f} (Max {risk_amount_account_currency:.2f})")
                     logging.info("\n".join(log_entries))
                     return Decimal('0.0')

                volume_final = volume_min_decimal
                log_entries.append(f"  11. Ajusté à Vol Min: {volume_final} lots (Risque réel: {loss_at_min_volume:.2f})")

            elif volume_rounded > Decimal(str(self.volume_max)):
                logging.warning(f"Volume calculé {volume_rounded} > Max {self.volume_max}. Ajusté à Max.")
                volume_final = Decimal(str(self.volume_max))
                loss_at_max_volume = (volume_final / Decimal('1.0')) * loss_per_lot
                log_entries.append(f"  11. Ajusté à Vol Max: {volume_final} lots (Risque réel: {loss_at_max_volume:.2f})")
            else:
                volume_final = volume_rounded
                loss_at_final_volume = (volume_final / Decimal('1.0')) * loss_per_lot
                log_entries.append(f"  11. Volume Final: {volume_final} lots (Risque réel: {loss_at_final_volume:.2f})")
            
            # --- Fin de la journalisation ---
            logging.info("\n".join(log_entries))
            return volume_final

        except InvalidOperation as e:
            logging.error(f"Erreur Decimal (InvalidOperation) dans _calculate_volume: {e}. Entrées: E={equity}, SL={sl_price}, P={entry_price}")
            log_entries.append(f"  ERREUR (InvalidOperation): {e}")
            logging.info("\n".join(log_entries))
            return Decimal('0.0')
        except Exception as e:
            logging.error(f"Erreur inattendue dans _calculate_volume: {e}", exc_info=True)
            log_entries.append(f"  ERREUR (Inattendue): {e}")
            logging.info("\n".join(log_entries))
            return Decimal('0.0')


    def _calculate_sl_tp_levels(self, entry_price: float, direction: str, ohlc_data: pd.DataFrame, trade_signal: dict) -> tuple:
        """
        Calcule les niveaux de SL et TP finaux en fonction de la stratégie
        configurée (SMC, ATR, etc.) et applique les buffers.
        """
        sl_price = 0.0
        tp_price = 0.0
        point = self.point
        digits = self.digits
        
        atr = self._calculate_atr(ohlc_data, 14) # ATR(14) par défaut

        # --- 1. Calcul du Stop Loss (SL) ---
        
        # Stratégie SMC (Structure/Invalidation)
        if self.sl_strategy == 'SMC_STRUCTURE' and 'sl_price' in trade_signal:
            sl_price = trade_signal['sl_price']
            logging.debug(f"SL {self.symbol} basé sur SMC_STRUCTURE: {sl_price}")
        
        # Stratégie ATR (Fallback ou défaut)
        if sl_price == 0.0: # Si SMC a échoué ou stratégie ATR
            if atr == 0.0:
                 logging.error(f"SL {self.symbol}: ATR est 0.0. Impossible de calculer SL dynamique. Annulation.")
                 return 0.0, 0.0
                 
            sl_distance = float(self.sl_atr_multiplier) * atr
            if direction == "BUY":
                sl_price = entry_price - sl_distance
            else: # SELL
                sl_price = entry_price + sl_distance
            logging.debug(f"SL {self.symbol} basé sur ATR({self.sl_atr_multiplier}) * {atr:.{digits}f} = {sl_distance:.{digits}f}. SL: {sl_price:.{digits}f}")

        # Application du Buffer SL (éloigne le SL du prix d'entrée)
        sl_buffer_abs = float(self.sl_buffer_pips) * point
        if direction == "BUY":
            sl_price -= sl_buffer_abs
        else: # SELL
            sl_price += sl_buffer_abs

        # --- 2. Calcul du Take Profit (TP) ---

        # Stratégie SMC (Liquidité/Target)
        if self.tp_strategy == 'SMC_LIQUIDITY_TARGET' and 'tp_price' in trade_signal:
            tp_price = trade_signal['tp_price']
            logging.debug(f"TP {self.symbol} basé sur SMC_LIQUIDITY_TARGET: {tp_price}")

        # Stratégie ATR (Fallback ou défaut)
        if tp_price == 0.0:
            if atr == 0.0:
                 logging.error(f"TP {self.symbol}: ATR est 0.0. Impossible de calculer TP dynamique. Annulation.")
                 return 0.0, 0.0 # SL pourrait être valide, mais TP échoue

            tp_distance = float(self.tp_atr_multiplier) * atr
            if direction == "BUY":
                tp_price = entry_price + tp_distance
            else: # SELL
                tp_price = entry_price - tp_distance
            logging.debug(f"TP {self.symbol} basé sur ATR({self.tp_atr_multiplier}) * {atr:.{digits}f} = {tp_distance:.{digits}f}. TP: {tp_price:.{digits}f}")

        # Application du Buffer TP (rapproche le TP du prix d'entrée)
        tp_buffer_abs = float(self.tp_buffer_pips) * point
        if direction == "BUY":
            tp_price -= tp_buffer_abs
        else: # SELL
            tp_price += tp_buffer_abs

        # --- 3. Validation finale et Arrondi ---
        
        # Assurer que SL/TP ne sont pas absurdes (ex: de l'autre côté de l'entrée)
        if direction == "BUY" and (sl_price >= entry_price or tp_price <= entry_price):
             logging.error(f"Erreur logique SL/TP (BUY) {self.symbol}: E={entry_price}, SL={sl_price}, TP={tp_price}. Annulation.")
             return 0.0, 0.0
        if direction == "SELL" and (sl_price <= entry_price or tp_price >= entry_price):
             logging.error(f"Erreur logique SL/TP (SELL) {self.symbol}: E={entry_price}, SL={sl_price}, TP={tp_price}. Annulation.")
             return 0.0, 0.0

        # Arrondi au nombre de digits requis par le broker
        sl_price = round(sl_price, digits)
        tp_price = round(tp_price, digits)

        return sl_price, tp_price


    def calculate_trade_parameters(self, equity: float, entry_price: float, ohlc_data: pd.DataFrame, trade_signal: dict) -> tuple:
        """
        Orchestre le calcul du SL/TP et du Volume.
        Retourne (volume, sl_price, tp_price)
        """
        direction = trade_signal['direction']
        
        # 1. Calculer SL/TP
        sl_price, tp_price = self._calculate_sl_tp_levels(entry_price, direction, ohlc_data, trade_signal)
        
        if sl_price == 0.0 or tp_price == 0.0:
            logging.error(f"Calcul SL/TP invalide pour {self.symbol}. Trade annulé.")
            return Decimal('0.0'), 0.0, 0.0

        # 2. Vérifier le risque total (si activé)
        if not self.check_max_concurrent_risk(equity):
             logging.warning(f"Risque concurrent maximal ({self.max_concurrent_risk_pct * 100}%) dépassé. Nouveau trade sur {self.symbol} bloqué.")
             return Decimal('0.0'), 0.0, 0.0 # Ne pas retourner SL/TP, juste volume 0

        # 3. Calculer Volume (basé sur SL)
        volume = self._calculate_volume(equity, sl_price, entry_price, direction)
        
        if volume <= 0:
             logging.warning(f"Calcul volume invalide ({volume}) pour {self.symbol}. Trade annulé.")
             return Decimal('0.0'), sl_price, tp_price # Retourner 0 volume

        return volume, sl_price, tp_price


    # --- Gestion des positions ouvertes ---

    def manage_open_positions(self, positions: list, tick, ohlc_data: pd.DataFrame):
        """
        Gère le PTP, BE et Trailing Stop pour les positions ouvertes
        sur CE symbole.
        
        *** CORRECTION v18.1.5: Annotation de type pour 'tick' supprimée ***
        """
        if not positions:
            return

        # 1. Gestion des prises de profit partielles (PTP)
        if self.ptp_rules:
            for rule in self.ptp_rules:
                rr_target = Decimal(str(rule.get('rr', 1.0)))
                percentage_to_close = Decimal(str(rule.get('percentage', 50.0))) / Decimal('100.0')
                self._apply_ptp(positions, tick, rr_target, percentage_to_close)

        # 2. Gestion du Breakeven
        if self.breakeven_rules.get('enabled', False):
            # Priorité: PTP peut déclencher BE (règle spécifique)
            if self.breakeven_rules.get('move_to_be_plus_on_ptp1', False) and self.ptp_rules:
                # Si PTP1 (le premier de la liste) a été exécuté
                ptp1_rr = Decimal(str(self.ptp_rules[0].get('rr', 1.0)))
                self._apply_breakeven_on_ptp(positions, tick, ptp1_rr)
            else:
                # BE standard basé sur Pips ou RR
                trigger_pips = self.breakeven_rules.get('trigger_pips', 0)
                if trigger_pips > 0:
                    self._apply_breakeven_pips(positions, tick, trigger_pips)

        # 3. Gestion du Trailing Stop (ATR)
        if self.trailing_stop_rules.get('enabled', False):
            activation_multiple = Decimal(str(self.trailing_stop_rules.get('activation_multiple', 2.0)))
            trailing_multiple = Decimal(str(self.trailing_stop_rules.get('trailing_multiple', 1.5)))
            atr = Decimal(str(self._calculate_atr(ohlc_data, 14)))
            if atr > 0:
                self._apply_trailing_stop_atr(positions, tick, atr, activation_multiple, trailing_multiple)


    def _apply_ptp(self, positions: list, tick, rr_target: Decimal, percentage_to_close: Decimal):
        """Logique de fermeture partielle basée sur RR."""
        for pos in positions:
            # Vérifier si ce PTP a déjà été appliqué (via le magic number ou commentaire ?)
            # Solution: utiliser le commentaire pour marquer les PTP
            if f"PTP{rr_target}" in pos.comment:
                continue # Ce PTP a déjà été appliqué

            initial_sl = pos.sl
            initial_entry = pos.price_open
            
            if pos.type == mt5.ORDER_TYPE_BUY:
                sl_distance = Decimal(str(initial_entry - initial_sl))
                if sl_distance <= 0: continue
                tp_target = Decimal(str(initial_entry)) + (sl_distance * rr_target)
                current_price = Decimal(str(tick.bid)) # Acheteur sort au Bid
                if current_price >= tp_target:
                    self._execute_partial_close(pos, percentage_to_close, f"PTP{rr_target}")
            
            elif pos.type == mt5.ORDER_TYPE_SELL:
                sl_distance = Decimal(str(initial_sl - initial_entry))
                if sl_distance <= 0: continue
                tp_target = Decimal(str(initial_entry)) - (sl_distance * rr_target)
                current_price = Decimal(str(tick.ask)) # Vendeur sort au Ask
                if current_price <= tp_target:
                    self._execute_partial_close(pos, percentage_to_close, f"PTP{rr_target}")


    def _execute_partial_close(self, position, percentage: Decimal, new_comment_flag: str):
        """Exécute la fermeture partielle via MT5Executor."""
        try:
            volume_to_close = Decimal(str(position.volume)) * percentage
            # Arrondir au step
            step = Decimal(str(self.volume_step))
            volume_to_close = (volume_to_close // step) * step
            
            # Vérifier si le volume restant est >= volume min
            volume_remaining = Decimal(str(position.volume)) - volume_to_close
            if volume_to_close < Decimal(str(self.volume_min)) or volume_remaining < Decimal(str(self.volume_min)):
                 logging.warning(f"PTP {position.ticket}: Volume à fermer ({volume_to_close}) ou restant ({volume_remaining}) < Min ({self.volume_min}). Fermeture totale ou annulation PTP?")
                 # Pour l'instant, on annule ce PTP s'il est trop petit
                 if volume_to_close < Decimal(str(self.volume_min)):
                     logging.info(f"PTP {position.ticket}: Annulé (volume < min).")
                     return 

            logging.info(f"PTP {new_comment_flag} atteint pour {self.symbol} (Ticket: {position.ticket}). Fermeture de {volume_to_close} lots.")
            
            # Mettre à jour le commentaire de la position restante
            new_comment = (position.comment or "") + f"|{new_comment_flag}"
            
            self.executor.close_partial_position(position, float(volume_to_close), new_comment)

        except Exception as e:
            logging.error(f"Erreur PTP (Ticket {position.ticket}): {e}", exc_info=True)


    def _apply_breakeven_pips(self, positions: list, tick, trigger_pips: int):
        """Déplace le SL à BE+pips si le trigger en pips est atteint."""
        pips_plus = self.breakeven_rules.get('pips_plus', 1.0)
        
        for pos in positions:
            if pos.sl == pos.price_open: continue # Déjà à BE (simple check)
            if "BE_APPLIED" in pos.comment: continue # Flag de commentaire
            
            trigger_distance_points = Decimal(str(trigger_pips)) * Decimal(str(self.point))
            sl_new_distance_points = Decimal(str(pips_plus)) * Decimal(str(self.point))
            
            if pos.type == mt5.ORDER_TYPE_BUY:
                current_profit_points = Decimal(str(tick.bid)) - Decimal(str(pos.price_open))
                if current_profit_points >= trigger_distance_points:
                    new_sl = round(float(Decimal(str(pos.price_open)) + sl_new_distance_points), self.digits)
                    # Ne pas déplacer le SL s'il est déjà meilleur
                    if new_sl > pos.sl:
                         logging.info(f"Breakeven (Pips) {self.symbol} (Ticket: {pos.ticket}). SL déplacé à {new_sl} (BE+{pips_plus} pips)")
                         self.executor.modify_position_sl_tp(pos.ticket, new_sl, pos.tp, "BE_APPLIED")
            
            elif pos.type == mt5.ORDER_TYPE_SELL:
                current_profit_points = Decimal(str(pos.price_open)) - Decimal(str(tick.ask))
                if current_profit_points >= trigger_distance_points:
                    new_sl = round(float(Decimal(str(pos.price_open)) - sl_new_distance_points), self.digits)
                    # Ne pas déplacer le SL s'il est déjà meilleur
                    if new_sl < pos.sl:
                         logging.info(f"Breakeven (Pips) {self.symbol} (Ticket: {pos.ticket}). SL déplacé à {new_sl} (BE+{pips_plus} pips)")
                         self.executor.modify_position_sl_tp(pos.ticket, new_sl, pos.tp, "BE_APPLIED")


    def _apply_breakeven_on_ptp(self, positions: list, tick, ptp1_rr: Decimal):
        """Déplace le SL à BE+pips (défini) si PTP1 (flag) est atteint."""
        pips_plus = self.breakeven_rules.get('pips_plus_on_ptp1', 5.0) # Ex: 5 pips
        
        for pos in positions:
             # Vérifie si le flag PTP1 est dans le commentaire ET si le flag BE n'y est PAS
            if f"PTP{ptp1_rr}" in pos.comment and "BE_APPLIED" not in pos.comment:
                sl_new_distance_points = Decimal(str(pips_plus)) * Decimal(str(self.point))
                
                if pos.type == mt5.ORDER_TYPE_BUY:
                    new_sl = round(float(Decimal(str(pos.price_open)) + sl_new_distance_points), self.digits)
                    if new_sl > pos.sl:
                         logging.info(f"Breakeven (Post-PTP1) {self.symbol} (Ticket: {pos.ticket}). SL déplacé à {new_sl} (BE+{pips_plus} pips)")
                         self.executor.modify_position_sl_tp(pos.ticket, new_sl, pos.tp, "BE_APPLIED")
                
                elif pos.type == mt5.ORDER_TYPE_SELL:
                    new_sl = round(float(Decimal(str(pos.price_open)) - sl_new_distance_points), self.digits)
                    if new_sl < pos.sl:
                         logging.info(f"Breakeakeven (Post-PTP1) {self.symbol} (Ticket: {pos.ticket}). SL déplacé à {new_sl} (BE+{pips_plus} pips)")
                         self.executor.modify_position_sl_tp(pos.ticket, new_sl, pos.tp, "BE_APPLIED")


    def _apply_trailing_stop_atr(self, positions: list, tick, atr: Decimal, activation_multiple: Decimal, trailing_multiple: Decimal):
        """Gère le trailing stop basé sur l'ATR."""
        if atr <= 0: return
        
        activation_distance_points = activation_multiple * atr
        trailing_distance_points = trailing_multiple * atr
        
        for pos in positions:
            if pos.type == mt5.ORDER_TYPE_BUY:
                current_price = Decimal(str(tick.bid))
                entry_price = Decimal(str(pos.price_open))
                current_profit_points = current_price - entry_price
                
                # Activation du Trailing
                if current_profit_points >= activation_distance_points:
                    # Calcul du nouveau SL
                    new_sl = float(current_price - trailing_distance_points)
                    new_sl = round(new_sl, self.digits)
                    
                    # Déplacer seulement si le nouveau SL est meilleur (plus haut)
                    if new_sl > pos.sl:
                         logging.debug(f"Trailing Stop (BUY) {self.symbol} (Ticket: {pos.ticket}). SL déplacé à {new_sl}")
                         self.executor.modify_position_sl_tp(pos.ticket, new_sl, pos.tp, "TS_APPLIED")
            
            elif pos.type == mt5.ORDER_TYPE_SELL:
                current_price = Decimal(str(tick.ask))
                entry_price = Decimal(str(pos.price_open))
                current_profit_points = entry_price - current_price

                # Activation du Trailing
                if current_profit_points >= activation_distance_points:
                    # Calcul du nouveau SL
                    new_sl = float(current_price + trailing_distance_points)
                    new_sl = round(new_sl, self.digits)
                    
                    # Déplacer seulement si le nouveau SL est meilleur (plus bas)
                    if new_sl < pos.sl:
                         logging.debug(f"Trailing Stop (SELL) {self.symbol} (Ticket: {pos.ticket}). SL déplacé à {new_sl}")
                         self.executor.modify_position_sl_tp(pos.ticket, new_sl, pos.tp, "TS_APPLIED")


    # --- Vérifications de risque globales ---

    def is_daily_loss_limit_reached(self) -> tuple:
        """Vérifie si la limite de perte journalière (en % equity) est atteinte."""
        try:
            magic_number = self.config['trading_settings'].get('magic_number', 0)
            
            # Définir le fuseau horaire du broker (config) ou UTC
            broker_tz_str = self.config.get('mt5_credentials', {}).get('timezone', 'UTC')
            broker_tz = pytz.timezone(broker_tz_str)
            
            # Heure de 'début de journée' du broker (ex: 00:00)
            day_start_str = self.config.get('risk_management', {}).get('daily_limit_reset_time_broker', '00:00')
            day_start_time = dt_time.fromisoformat(day_start_str)

            # Calculer le début de la journée de trading actuelle
            now_broker_time = datetime.now(broker_tz)
            start_of_today = broker_tz.localize(datetime(now_broker_time.year, now_broker_time.month, now_broker_time.day, day_start_time.hour, day_start_time.minute))
            
            # Si l'heure actuelle est avant le reset, 'aujourd'hui' a commencé hier
            if now_broker_time < start_of_today:
                start_of_today = start_of_today - timedelta(days=1)
                
            # Convertir en UTC pour MT5
            start_of_today_utc = start_of_today.astimezone(pytz.utc)
            now_utc = datetime.now(pytz.utc)

            # Obtenir les deals (trades clos) depuis le début de la journée
            deals = self.mt5.history_deals_get(start_of_today_utc, now_utc)
            
            if deals is None:
                logging.error("Impossible de récupérer l'historique des deals (daily loss check).")
                return False, Decimal('0.0')

            total_profit_today = Decimal('0.0')
            for deal in deals:
                if deal.magic == magic_number and deal.entry == mt5.DEAL_ENTRY_OUT: # Seulement les sorties
                     total_profit_today += Decimal(str(deal.profit)) + Decimal(str(deal.commission)) + Decimal(str(deal.swap))

            # Obtenir le P/L flottant actuel
            floating_pl = self.executor.get_total_floating_pl(magic_number)
            
            # *** CORRECTION v18.1.6: Convertir float en Decimal avant addition ***
            total_current_pl = total_profit_today + Decimal(str(floating_pl))
            
            equity = Decimal(str(self.account_info.equity))
            loss_limit_amount = self.daily_loss_limit_pct * equity
            
            current_loss = -total_current_pl if total_current_pl < 0 else Decimal('0.0')

            if current_loss > loss_limit_amount:
                logging.critical(f"LIMITE PERTE JOUR ATTEINTE: Perte actuelle {current_loss:.2f} {self.account_currency} > Limite {loss_limit_amount:.2f} {self.account_currency}")
                return True, current_loss
            
            logging.info(f"Check Perte Jour: {current_loss:.2f} / {loss_limit_amount:.2f} {self.account_currency}")
            return False, current_loss

        except Exception as e:
            logging.error(f"Erreur vérification limite perte jour: {e}", exc_info=True)
            return False, Decimal('0.0') # Prudence: ne pas bloquer si erreur


    def check_max_concurrent_risk(self, equity: float) -> bool:
        """
        Vérifie si l'ajout d'un nouveau trade (au risque standard)
        dépasse le risque total simultané autorisé.
        """
        if self.max_concurrent_risk_pct <= 0: # Fonction désactivée
            return True 

        try:
            magic_number = self.config['trading_settings'].get('magic_number', 0)
            open_positions = self.executor.get_open_positions(magic_number)
            
            current_total_risk_pct = Decimal('0.0')
            
            # Calculer le risque actuel des positions ouvertes
            # C'est complexe car le SL peut avoir bougé.
            # Simplification: On suppose que chaque trade ouvert = 1 R (self.risk_per_trade_pct)
            # Sauf si le SL est à BE ou en profit.
            
            for pos in open_positions:
                 is_at_be_or_profit = False
                 if pos.type == mt5.ORDER_TYPE_BUY and pos.sl > pos.price_open:
                     is_at_be_or_profit = True
                 elif pos.type == mt5.ORDER_TYPE_SELL and pos.sl < pos.price_open:
                     is_at_be_or_profit = True
                     
                 if not is_at_be_or_profit:
                     # On pourrait recalculer le risque exact basé sur le SL actuel...
                     # Simplification:
                     current_total_risk_pct += self.risk_per_trade_pct

            # Risque potentiel après ajout du nouveau trade
            potential_total_risk = current_total_risk_pct + self.risk_per_trade_pct
            
            limit_pct = self.max_concurrent_risk_pct
            
            if potential_total_risk > limit_pct:
                 logging.warning(f"Check Risque Concurrent: {potential_total_risk*100:.1f}% (Actuel: {current_total_risk_pct*100:.1f}%) > Limite {limit_pct*100:.1f}%.")
                 return False
            
            logging.info(f"Check Risque Concurrent: {potential_total_risk*100:.1f}% <= Limite {limit_pct*100:.1f}%.")
            return True

        except Exception as e:
             logging.error(f"Erreur vérification risque concurrent: {e}", exc_info=True)
             return False # Prudence: bloquer le trade si erreur