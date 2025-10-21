# Fichier: src/risk/risk_manager.py
# Version: 17.0.2 (Risk-Percent-Fix) # <-- Version mise à jour
# Dépendances: MetaTrader5, pandas, numpy, logging, pytz, src.constants

import MetaTrader5 as mt5
import logging
import math
import pandas as pd
import numpy as np
from datetime import datetime
import pytz
from typing import Tuple, List, Dict, Optional

from src.constants import BUY, SELL

class RiskManager:
    """
    Gère le risque avec une stratégie de TP configurable et une validation des signaux.
    v17.0.2: Corrige le calcul du pourcentage de risque.
    """
    def __init__(self, config: dict, executor, symbol: str):
        self.log = logging.getLogger(self.__class__.__name__)
        self._config: Dict = config
        self._executor = executor
        self._symbol: str = symbol

        self.symbol_info = self._executor._mt5.symbol_info(self._symbol)
        self.account_info = self._executor._mt5.account_info()

        if not self.symbol_info or not self.account_info:
            self.log.critical(f"Impossible d'obtenir les informations pour le symbole {self._symbol} ou pour le compte.")
            raise ValueError("Informations de compte ou de symbole MT5 manquantes.")

        self.point: float = self.symbol_info.point
        self.digits: int = self.symbol_info.digits

    def is_daily_loss_limit_reached(self) -> Tuple[bool, float]:
        risk_settings = self._config.get('risk_management', {})
        loss_limit_percent_config = risk_settings.get('daily_loss_limit_percent', 2.0) # Valeur lue comme 2.0
        if loss_limit_percent_config <= 0:
            return False, 0.0

        try:
            # S'assurer que le compte info est à jour
            current_account_info = self._executor.get_account_info() # Utiliser la méthode de l'executor
            if not current_account_info:
                 self.log.warning("Impossible de récupérer les informations de compte actuelles pour la limite de perte.")
                 return False, 0.0 # Ne pas bloquer si info compte échoue temporairement

            today_start_utc = datetime.now(pytz.utc).replace(hour=0, minute=0, second=0, microsecond=0)
            history_deals = self._executor._mt5.history_deals_get(today_start_utc, datetime.now(pytz.utc))

            if history_deals is None:
                self.log.warning("Impossible de récupérer l'historique des transactions pour la limite de perte journalière.")
                return False, 0.0

            magic_number = self._config.get('trading_settings', {}).get('magic_number', 0)
            # Somme des profits des deals de SORTIE (entry==1) uniquement
            daily_pnl = sum(deal.profit for deal in history_deals if deal.magic == magic_number and deal.entry == 1) # DEAL_ENTRY_OUT

            # Calcul de la limite en montant basé sur l'equity ACTUELLE
            loss_limit_amount = (current_account_info.equity * loss_limit_percent_config) / 100.0

            if daily_pnl < 0 and abs(daily_pnl) >= loss_limit_amount:
                self.log.critical(f"ARRÊT D'URGENCE: Perte journalière ({daily_pnl:.2f}) a atteint la limite de {loss_limit_percent_config}% ({loss_limit_amount:.2f}).")
                return True, daily_pnl

            # Log informatif si la perte approche la limite (ex: > 80%)
            elif daily_pnl < 0 and abs(daily_pnl) >= loss_limit_amount * 0.8:
                 self.log.warning(f"ATTENTION: Perte journalière ({daily_pnl:.2f}) approche la limite de {loss_limit_percent_config}% ({loss_limit_amount:.2f}).")


            return False, daily_pnl
        except Exception as e:
            self.log.error(f"Erreur critique dans le calcul de la limite de perte journalière : {e}", exc_info=True)
            return False, 0.0 # En cas d'erreur, ne pas bloquer le trading par défaut

    def calculate_trade_parameters(self, equity: float, price: float, ohlc_data: pd.DataFrame, trade_signal: dict) -> Tuple[float, float, float]:
        try:
            if not isinstance(trade_signal, dict) or 'direction' not in trade_signal:
                self.log.error(f"Signal de trade invalide reçu: {trade_signal}. 'direction' manquante.")
                return 0.0, 0.0, 0.0

            # --- CORRECTION ICI (lecture du % risque) ---
            # Lire la valeur depuis config (ex: 1)
            risk_percent_config = self._config.get('risk_management', {}).get('risk_per_trade', 1.0) # Valeur par défaut 1.0 si non trouvée
            # Convertir en décimal pour le calcul (ex: 1 -> 0.01)
            risk_decimal = risk_percent_config / 100.0
            # --- FIN CORRECTION ---

            # Vérifier si ohlc_data n'est pas vide avant de l'utiliser
            if ohlc_data is None or ohlc_data.empty:
                 self.log.error("Données OHLC vides fournies pour le calcul SL/TP. Trade annulé.")
                 return 0.0, 0.0, 0.0

            ideal_sl, ideal_tp = self._calculate_initial_sl_tp(price, ohlc_data, trade_signal)

            if ideal_sl == 0 or ideal_tp == 0 or abs(price - ideal_sl) < self.symbol_info.point * 2: # SL/TP valides et distance minimale
                self.log.error(f"SL/TP invalide ou trop serré calculé. SL: {ideal_sl}, TP: {ideal_tp}, Prix: {price}. Trade annulé.")
                return 0.0, 0.0, 0.0

            # Utiliser risk_decimal pour le calcul de volume
            ideal_volume = self._calculate_volume(equity, risk_decimal, price, ideal_sl)

            if ideal_volume == 0.0:
                 self.log.warning(f"Calcul de volume a retourné 0.0. Vérifiez les logs de _calculate_volume. Trade annulé.")
                 return 0.0, 0.0, 0.0
            elif ideal_volume < self.symbol_info.volume_min:
                self.log.warning(f"Le volume idéal calculé ({ideal_volume:.4f}) est inférieur au min ({self.symbol_info.volume_min}). Trade annulé.")
                return 0.0, 0.0, 0.0
            elif ideal_volume > self.symbol_info.volume_max:
                 self.log.warning(f"Le volume idéal calculé ({ideal_volume:.4f}) dépasse le max ({self.symbol_info.volume_max}). Ajustement au maximum.")
                 ideal_volume = self.symbol_info.volume_max # Plafonner au max autorisé

            return ideal_volume, ideal_sl, ideal_tp

        except Exception as e:
            self.log.error(f"Erreur inattendue lors du calcul des paramètres de trade : {e}", exc_info=True)
            return 0.0, 0.0, 0.0

    # --- CORRECTION DANS _calculate_volume ---
    # Le paramètre risk_percent est maintenant attendu en décimal (ex: 0.01 pour 1%)
    def _calculate_volume(self, equity: float, risk_decimal: float, entry_price: float, sl_price: float) -> float:
        self.log.debug("--- DÉBUT DU CALCUL DE VOLUME SÉCURISÉ ---")

        # risk_decimal est déjà la fraction (ex: 0.01)
        risk_amount_account_currency = equity * risk_decimal
        self.log.debug(f"1. Capital: {equity:.2f} | Risque: {risk_decimal:.2%} -> Montant à risquer: {risk_amount_account_currency:.2f} {self.account_info.currency}")

        sl_distance_price = abs(entry_price - sl_price)
        if sl_distance_price < self.point: # Vérifier distance SL non nulle
            self.log.error("Distance du SL quasi nulle ou négative. Annulation du calcul de volume.")
            return 0.0
        self.log.debug(f"2. Distance SL: {sl_distance_price:.{self.digits}f} (en prix de l'actif)")

        # Vérifier si trade_contract_size est valide
        contract_size = self.symbol_info.trade_contract_size
        if not contract_size or contract_size <= 0:
            self.log.error(f"Taille de contrat invalide ({contract_size}) pour {self._symbol}. Annulation calcul volume.")
            return 0.0

        loss_per_lot_profit_currency = sl_distance_price * contract_size
        profit_currency = self.symbol_info.currency_profit
        self.log.debug(f"3. Perte/Lot ({contract_size} unités) en devise de profit ({profit_currency}): {loss_per_lot_profit_currency:.2f}")

        loss_per_lot_account_currency = loss_per_lot_profit_currency
        account_currency = self.account_info.currency
        if profit_currency != account_currency:
            conversion_rate = self.get_conversion_rate(profit_currency, account_currency)
            if not conversion_rate or conversion_rate <= 0:
                self.log.error(f"Impossible d'obtenir un taux de conversion valide pour {profit_currency}->{account_currency}. Annulation.")
                return 0.0
            loss_per_lot_account_currency *= conversion_rate
            self.log.debug(f"4. Conversion: {profit_currency}/{account_currency} @ {conversion_rate:.5f} -> Perte/Lot en devise du compte: {loss_per_lot_account_currency:.2f}")
        else:
            self.log.debug("4. Pas de conversion de devise nécessaire.")

        if loss_per_lot_account_currency <= 0:
            self.log.error("La perte par lot calculée est nulle ou négative après conversion. Annulation.")
            return 0.0

        # Calcul du volume brut
        volume = risk_amount_account_currency / loss_per_lot_account_currency
        self.log.debug(f"5. Volume brut: {risk_amount_account_currency:.2f} / {loss_per_lot_account_currency:.2f} = {volume:.4f} lots")

        # Ajustement au step de volume
        volume_step = self.symbol_info.volume_step
        if volume_step > 0:
            # Arrondir à l'inférieur au step le plus proche
            volume = math.floor(volume / volume_step) * volume_step
            # Arrondir à N décimales pour éviter pbs de float (ex: 0.010000000002)
            # Déterminer le nombre de décimales du step (ex: 0.01 -> 2)
            step_decimals = 0
            if '.' in str(volume_step):
                 step_decimals = len(str(volume_step).split('.')[-1])
            volume = round(volume, step_decimals)

        else:
             # Si step est 0, on ne peut pas ajuster, potentiellement pb config broker
             self.log.warning(f"Volume step est zéro ou invalide ({volume_step}) pour {self._symbol}, impossible d'ajuster le volume.")
             # On pourrait retourner 0.0 ou essayer de continuer avec le volume brut mais c'est risqué
             return 0.0 # Plus sûr de retourner 0

        # Vérifier min/max après ajustement au step
        final_volume = max(0, min(self.symbol_info.volume_max, volume))

        # Log final avec toutes les contraintes
        self.log.debug(f"6. Volume final ajusté: {final_volume:.4f} (Contraintes: Min={self.symbol_info.volume_min}, Max={self.symbol_info.volume_max}, Step={volume_step})")
        self.log.debug("--- FIN DU CALCUL DE VOLUME ---")
        return final_volume

    # --- _calculate_initial_sl_tp nécessite ohlc_data et trade_signal maintenant ---
    def _calculate_initial_sl_tp(self, price: float, ohlc_data: pd.DataFrame, trade_signal: dict) -> Tuple[float, float]:
        rm_settings = self._config.get('risk_management', {})
        sl_strategy = rm_settings.get('sl_strategy', 'ATR_MULTIPLE') # Stratégie SL peut différer de TP
        tp_strategy = rm_settings.get('tp_strategy', 'ATR_MULTIPLE')
        direction = trade_signal['direction']

        # Calculer l'ATR (nécessaire pour plusieurs stratégies)
        atr_settings_global = rm_settings.get('atr_settings', {}).get('default', {})
        # Vérifier s'il y a des paramètres ATR spécifiques au symbole
        atr_settings_symbol = rm_settings.get('atr_settings', {}).get(self._symbol, {})
        # Fusionner: les clés spécifiques au symbole écrasent les clés par défaut
        atr_settings = {**atr_settings_global, **atr_settings_symbol}

        period = atr_settings.get('period', 14)
        atr = self.calculate_atr(ohlc_data, period)
        if atr is None or atr <= 0:
            self.log.error(f"ATR invalide (période {period}) pour {self._symbol}. Impossible de calculer SL/TP basé sur ATR.")
            # Ne retourne pas 0, 0 immédiatement, certaines stratégies n'utilisent pas ATR
            # return 0.0, 0.0 <-- Commenté

        sl, tp = 0.0, 0.0

        # --- Calcul du Stop Loss (SL) ---
        if sl_strategy == "ATR_MULTIPLE":
            if atr is None or atr <= 0: return 0.0, 0.0 # ATR est requis ici
            sl_multiple = atr_settings.get('sl_multiple', 1.5)
            sl_distance = atr * sl_multiple
            sl = price - sl_distance if direction == BUY else price + sl_distance
            self.log.debug(f"Stratégie SL ATR: Multiple={sl_multiple}, ATR={atr:.5f}, Distance={sl_distance:.5f}, SL calculé à {sl:.{self.digits}f}")
        elif sl_strategy == "SMC_STRUCTURE":
            # Trouver le dernier swing high/low pertinent avant le prix actuel
            swing_highs, swing_lows = self._find_swing_points(ohlc_data.iloc[:-1].copy(), n=3) # Exclure la bougie actuelle
            if direction == BUY:
                 if not swing_lows.empty:
                      # Prendre le plus bas des N derniers swing lows (ex: 2) comme structure
                      relevant_low = swing_lows['low'].tail(2).min()
                      sl = relevant_low
                      self.log.debug(f"Stratégie SL SMC Structure (BUY): Dernier low pertinent à {sl:.{self.digits}f}")
                 else:
                      self.log.warning("Strat SL SMC Structure (BUY) mais aucun swing low trouvé. Utilisation ATR par défaut.")
                      if atr is None or atr <= 0: return 0.0, 0.0
                      sl_multiple = atr_settings.get('sl_multiple', 1.5)
                      sl = price - (atr * sl_multiple)
            else: # SELL
                 if not swing_highs.empty:
                      relevant_high = swing_highs['high'].tail(2).max()
                      sl = relevant_high
                      self.log.debug(f"Stratégie SL SMC Structure (SELL): Dernier high pertinent à {sl:.{self.digits}f}")
                 else:
                      self.log.warning("Strat SL SMC Structure (SELL) mais aucun swing high trouvé. Utilisation ATR par défaut.")
                      if atr is None or atr <= 0: return 0.0, 0.0
                      sl_multiple = atr_settings.get('sl_multiple', 1.5)
                      sl = price + (atr * sl_multiple)
            # Ajouter un buffer optionnel au SL basé sur la structure
            sl_buffer_pips = rm_settings.get('sl_buffer_pips', 5)
            sl_buffer = sl_buffer_pips * self.point
            sl = sl - sl_buffer if direction == BUY else sl + sl_buffer
            self.log.debug(f"SL après ajout buffer ({sl_buffer_pips} pips): {sl:.{self.digits}f}")

        else:
            self.log.error(f"La stratégie SL '{sl_strategy}' n'est pas reconnue. Impossible de calculer le SL.")
            return 0.0, 0.0


        # --- Calcul du Take Profit (TP) ---
        if tp_strategy == "ATR_MULTIPLE":
             if atr is None or atr <= 0: return sl, 0.0 # ATR requis, mais SL peut être valide
             tp_multiple = atr_settings.get('tp_multiple', 3.0)
             tp_distance = atr * tp_multiple
             tp = price + tp_distance if direction == BUY else price - tp_distance
             self.log.debug(f"Stratégie TP ATR: Multiple={tp_multiple}, Distance={tp_distance:.5f}, TP calculé à {tp:.{self.digits}f}")

        elif tp_strategy == "SMC_LIQUIDITY_TARGET":
            target = trade_signal.get('target_price')
            if not target or not isinstance(target, (float, int)):
                self.log.error("Stratégie TP SMC Cible choisie mais aucune cible ('target_price' valide) trouvée dans le signal.")
                # Fallback sur ATR si possible
                if atr is not None and atr > 0:
                     tp_multiple = atr_settings.get('tp_multiple', 3.0)
                     tp_distance = atr * tp_multiple
                     tp = price + tp_distance if direction == BUY else price - tp_distance
                     self.log.warning(f"Utilisation TP ATR ({tp_multiple}x) par défaut: {tp:.{self.digits}f}")
                else:
                     return sl, 0.0 # Impossible de définir un TP
            else:
                 tp = target
                 self.log.debug(f"Stratégie TP SMC Cible: Cible de liquidité à {tp:.{self.digits}f}")

        else:
            self.log.error(f"La stratégie TP '{tp_strategy}' n'est pas reconnue. Impossible de calculer le TP.")
            return sl, 0.0 # Retourner SL valide si calculé, mais TP=0

        # Vérification finale de validité et ratio
        if sl == 0 or tp == 0:
             self.log.error(f"Calcul final SL ({sl}) ou TP ({tp}) est zéro.")
             return 0.0, 0.0 # Invalide

        sl_dist_final = abs(price - sl)
        tp_dist_final = abs(tp - price)

        if sl_dist_final < self.point * 2: # Éviter SL trop proche
             self.log.error(f"SL final ({sl:.{self.digits}f}) est trop proche du prix ({price:.{self.digits}f}).")
             return 0.0, 0.0
        if tp_dist_final < self.point * 2: # Éviter TP trop proche
             self.log.error(f"TP final ({tp:.{self.digits}f}) est trop proche du prix ({price:.{self.digits}f}).")
             return sl, 0.0 # Garder SL si valide

        if tp_dist_final < sl_dist_final:
            self.log.warning(f"Le TP final est plus proche que le SL (Ratio < 1). TP={tp:.{self.digits}f}, SL={sl:.{self.digits}f}")

        return round(sl, self.digits), round(tp, self.digits)

    # --- Ajout _find_swing_points (utilisé par SL SMC) ---
    def _find_swing_points(self, df: pd.DataFrame, n: int = 2):
        """Trouve les swing highs et swing lows. n=nb bougies avant/après."""
        # Shift pour éviter d'utiliser les données futures lors de la recherche
        df['is_swing_high'] = df['high'] == df['high'].rolling(window=2*n+1, center=True, min_periods=n+1).max()
        df['is_swing_low'] = df['low'] == df['low'].rolling(window=2*n+1, center=True, min_periods=n+1).min()
        # Correction pour éviter la détection multiple sur plateaux
        df.loc[df['is_swing_high'].shift(1) & df['is_swing_high'], 'is_swing_high'] = False
        df.loc[df['is_swing_low'].shift(1) & df['is_swing_low'], 'is_swing_low'] = False

        swing_highs = df[df['is_swing_high']]
        swing_lows = df[df['is_swing_low']]
        return swing_highs, swing_lows

    def get_conversion_rate(self, from_currency: str, to_currency: str) -> Optional[float]:
        if from_currency == to_currency: return 1.0

        # Tenter la paire directe
        pair1 = f"{from_currency}{to_currency}"
        info1 = self._executor._mt5.symbol_info_tick(pair1)
        # Utiliser ASK pour acheter la devise de base (from_currency)
        if info1 and info1.ask > 0:
            self.log.debug(f"Taux de change direct trouvé pour {pair1}: Ask={info1.ask}")
            return info1.ask

        # Tenter la paire inverse
        pair2 = f"{to_currency}{from_currency}"
        info2 = self._executor._mt5.symbol_info_tick(pair2)
        # Utiliser BID pour vendre la devise de base (to_currency) -> 1/BID
        if info2 and info2.bid > 0:
            self.log.debug(f"Taux de change inverse trouvé pour {pair2}: Bid={info2.bid}. Taux calculé: {1.0 / info2.bid}")
            return 1.0 / info2.bid

        # Tenter via une devise pivot (USD, EUR, GBP)
        for pivot in ["USD", "EUR", "GBP"]:
             if from_currency != pivot and to_currency != pivot:
                 # Ex: Convertir JPY en CHF via USD
                 # Taux 1: from_currency -> pivot (ex: JPY -> USD, on vend USDJPY -> BID)
                 rate1 = self.get_conversion_rate(from_currency, pivot)
                 # Taux 2: pivot -> to_currency (ex: USD -> CHF, on achète USDCHF -> ASK)
                 rate2 = self.get_conversion_rate(pivot, to_currency)

                 if rate1 and rate2:
                     cross_rate = rate1 * rate2
                     self.log.debug(f"Taux de change croisé trouvé via {pivot} ({from_currency}->{pivot} * {pivot}->{to_currency}): {rate1:.5f} * {rate2:.5f} = {cross_rate:.5f}")
                     return cross_rate

        self.log.error(f"Impossible de trouver une paire de conversion valide pour {from_currency} -> {to_currency}")
        return None

    def calculate_atr(self, ohlc_data: pd.DataFrame, period: int) -> Optional[float]:
        if ohlc_data is None or ohlc_data.empty or len(ohlc_data) < period + 1: # Besoin d'au moins period+1 pour shift()
            self.log.warning(f"Données OHLC insuffisantes pour calculer ATR sur {period} périodes (reçu: {len(ohlc_data)}).")
            return None

        # Copier pour éviter SettingWithCopyWarning
        df = ohlc_data.copy()

        # Calcul du True Range
        df['high_low'] = df['high'] - df['low']
        df['high_close_prev'] = np.abs(df['high'] - df['close'].shift(1))
        df['low_close_prev'] = np.abs(df['low'] - df['close'].shift(1))

        df['true_range'] = df[['high_low', 'high_close_prev', 'low_close_prev']].max(axis=1)

        # Calcul de l'ATR (EMA du True Range)
        # Utiliser ewm() pour EMA standard
        atr = df['true_range'].ewm(span=period, adjust=False).mean().iloc[-1]

        if atr is None or pd.isna(atr) or atr <= 0:
             self.log.warning(f"Calcul ATR invalide ({atr}) pour {self._symbol} sur {period} périodes.")
             return None

        return atr

    def manage_open_positions(self, positions: list, current_tick, ohlc_data: pd.DataFrame):
        if not positions or not current_tick or ohlc_data is None or ohlc_data.empty:
             # self.log.debug("Pas de positions, tick, ou données OHLC pour manage_open_positions.")
             return

        risk_settings = self._config.get('risk_management', {})

        # 1. Gestion du Break-even
        be_cfg = risk_settings.get('breakeven', {})
        if be_cfg.get('enabled', False):
            self._apply_breakeven(positions, current_tick, be_cfg)

        # 2. Gestion du Trailing Stop ATR
        ts_atr_cfg = risk_settings.get('trailing_stop_atr', {})
        if ts_atr_cfg.get('enabled', False):
            self._apply_trailing_stop_atr(positions, current_tick, ohlc_data, risk_settings) # Passer risk_settings entier pour ATR

        # 3. Gestion des TPs Partiels (Optionnel)
        partial_tp_cfg = risk_settings.get('partial_tp', {})
        if partial_tp_cfg.get('enabled', False):
             # Logique à implémenter si nécessaire
             pass

    def _apply_breakeven(self, positions: list, tick, be_cfg: dict):
        trigger_pips = be_cfg.get('trigger_pips', 100) # Ex: 100 pips
        pips_plus = be_cfg.get('pips_plus', 10) # Ex: Mettre SL à +10 pips

        trigger_distance = trigger_pips * self.point
        plus_distance = pips_plus * self.point

        for pos in positions:
            pnl_distance = 0.0
            new_sl_target = 0.0
            is_already_at_be_or_better = False

            if pos.type == mt5.ORDER_TYPE_BUY:
                pnl_distance = tick.bid - pos.price_open
                new_sl_target = pos.price_open + plus_distance
                is_already_at_be_or_better = pos.sl >= pos.price_open # Vérifier si SL est déjà >= prix d'entrée

            elif pos.type == mt5.ORDER_TYPE_SELL:
                pnl_distance = pos.price_open - tick.ask
                new_sl_target = pos.price_open - plus_distance
                is_already_at_be_or_better = pos.sl != 0 and pos.sl <= pos.price_open # Vérifier si SL est déjà <= prix d'entrée (et non 0)

            # Déclencher BE si:
            # 1. Le profit atteint le seuil de déclenchement
            # 2. Le SL n'est PAS déjà à break-even ou en profit
            if pnl_distance >= trigger_distance and not is_already_at_be_or_better:
                # Vérifier si le nouveau SL est valide (pas trop proche, etc.)
                # Pour BUY, new_sl > price_open. Pour SELL, new_sl < price_open.
                # Il faut aussi s'assurer qu'il n'est pas "plus loin" que le SL actuel si SL actuel est déjà en profit
                # Mais la condition 'is_already_at_be_or_better' gère ce cas.

                # Vérification simple que le SL cible est différent du SL actuel
                if abs(new_sl_target - pos.sl) > self.point / 2: # Eviter modifs inutiles pour qques fractions
                    self.log.info(f"BREAK-EVEN déclenché pour ticket #{pos.ticket} ({pnl_distance/self.point:.1f} >= {trigger_pips} pips). Nouveau SL: {new_sl_target:.{self.digits}f}")
                    self._executor.modify_position(pos.ticket, new_sl_target, pos.tp)


    def _apply_trailing_stop_atr(self, positions: list, tick, ohlc_data: pd.DataFrame, risk_cfg: dict):
        ts_cfg = risk_cfg.get('trailing_stop_atr', {})
        # Utiliser les paramètres ATR (symbol ou default) comme dans _calculate_initial_sl_tp
        atr_settings_global = risk_cfg.get('atr_settings', {}).get('default', {})
        atr_settings_symbol = risk_cfg.get('atr_settings', {}).get(self._symbol, {})
        atr_settings = {**atr_settings_global, **atr_settings_symbol}

        period = atr_settings.get('period', 14)
        atr = self.calculate_atr(ohlc_data, period)
        if atr is None or atr <= 0:
            # self.log.debug("ATR invalide pour Trailing Stop.")
            return # Ne pas continuer si ATR invalide

        activation_multiple = ts_cfg.get('activation_multiple', 2.0) # Ex: Déclencher si profit > 2 * ATR
        trailing_multiple = ts_cfg.get('trailing_multiple', 1.8) # Ex: Mettre SL à 1.8 * ATR du prix actuel

        activation_distance = atr * activation_multiple
        trailing_distance = atr * trailing_multiple

        for pos in positions:
            new_sl = pos.sl # Garder l'ancien SL par défaut
            pnl_distance = 0.0

            if pos.type == mt5.ORDER_TYPE_BUY:
                pnl_distance = tick.bid - pos.price_open
                # Condition d'activation: si le profit actuel dépasse la distance d'activation
                if pnl_distance >= activation_distance:
                    # Calculer le nouveau SL potentiel basé sur le prix actuel (bid)
                    potential_new_sl = tick.bid - trailing_distance
                    # Condition de mise à jour: si le nouveau SL est meilleur (plus haut) que l'ancien SL
                    # (ou si l'ancien SL était zéro)
                    if pos.sl == 0 or potential_new_sl > pos.sl:
                        new_sl = potential_new_sl

            elif pos.type == mt5.ORDER_TYPE_SELL:
                pnl_distance = pos.price_open - tick.ask
                if pnl_distance >= activation_distance:
                    potential_new_sl = tick.ask + trailing_distance
                    # Condition de mise à jour: si le nouveau SL est meilleur (plus bas) que l'ancien SL
                    # (ou si l'ancien SL était zéro)
                    if pos.sl == 0 or potential_new_sl < pos.sl:
                        new_sl = potential_new_sl

            # Si le SL a été modifié ET est différent du SL actuel (avec une petite marge)
            if new_sl != pos.sl and abs(new_sl - pos.sl) > self.point / 2:
                self.log.info(f"TRAILING STOP ATR (ATR={atr:.5f}, Mult={trailing_multiple}) pour #{pos.ticket}. Nouveau SL: {new_sl:.{self.digits}f}")
                self._executor.modify_position(pos.ticket, new_sl, pos.tp)