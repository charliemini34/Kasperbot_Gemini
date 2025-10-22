# Fichier: src/risk/risk_manager.py
# Version: 1.1.0 (Risk-2)
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
    Gère le risque avec TP partiels, BE, Trailing, et calcul de volume.
    v1.1.0: Ajout ébauche logique TP partiels.
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
        # Stocker l'état des TP partiels pris (pour éviter prises multiples)
        # Clé: ticket de position, Valeur: set des niveaux RR atteints (ex: {1, 2})
        self._partial_tp_taken = {}

    def is_daily_loss_limit_reached(self) -> Tuple[bool, float]:
        risk_settings = self._config.get('risk_management', {})
        loss_limit_percent = risk_settings.get('daily_loss_limit_percent', 2.0)
        if loss_limit_percent <= 0:
            return False, 0.0

        try:
            # Utiliser UTC pour la date de début
            today_start_utc = datetime.now(pytz.utc).replace(hour=0, minute=0, second=0, microsecond=0)
            now_utc = datetime.now(pytz.utc)
            history_deals = self._executor._mt5.history_deals_get(today_start_utc, now_utc)

            if history_deals is None:
                self.log.warning("Impossible de récupérer l'historique des transactions pour la limite de perte journalière.")
                return False, 0.0

            magic_number = self._config.get('trading_settings', {}).get('magic_number', 0)
            # Somme des profits des deals de sortie (entry=1) ou INOUT (entry=2?) pour le magic number
            daily_pnl = sum(deal.profit for deal in history_deals if deal.magic == magic_number and deal.entry in [mt5.DEAL_ENTRY_OUT, mt5.DEAL_ENTRY_INOUT])


            equity_now = self.account_info.equity # Utiliser l'équité actuelle
            loss_limit_amount = (equity_now * loss_limit_percent) / 100.0

            if daily_pnl < 0 and abs(daily_pnl) >= loss_limit_amount:
                self.log.critical(f"ARRÊT D'URGENCE: Perte journalière ({daily_pnl:.2f}) a atteint la limite de {loss_limit_percent}% ({loss_limit_amount:.2f}).")
                return True, daily_pnl

            # Log PNL journalier même s'il n'atteint pas la limite
            # self.log.debug(f"PNL journalier actuel: {daily_pnl:.2f} {self.account_info.currency}")
            return False, daily_pnl
        except Exception as e:
            self.log.error(f"Erreur critique dans le calcul de la limite de perte journalière : {e}", exc_info=True)
            return False, 0.0


    def calculate_trade_parameters(self, equity: float, price: float, ohlc_data: pd.DataFrame, trade_signal: dict) -> Tuple[float, float, float]:
        # --- Fonction inchangée ---
        try:
            if not isinstance(trade_signal, dict) or 'direction' not in trade_signal:
                self.log.error(f"Signal de trade invalide reçu: {trade_signal}. 'direction' manquante.")
                return 0.0, 0.0, 0.0

            risk_percent = self._config.get('risk_management', {}).get('risk_per_trade', 0.01)

            ideal_sl, ideal_tp = self._calculate_initial_sl_tp(price, ohlc_data, trade_signal)

            # Ajouter buffer au SL si configuré
            sl_buffer_pips = self._config.get('risk_management', {}).get('sl_buffer_pips', 0)
            if sl_buffer_pips > 0 and ideal_sl != 0:
                 sl_buffer = sl_buffer_pips * self.point
                 ideal_sl = ideal_sl - sl_buffer if trade_signal['direction'] == BUY else ideal_sl + sl_buffer
                 ideal_sl = round(ideal_sl, self.digits)
                 self.log.debug(f"SL ajusté avec buffer de {sl_buffer_pips} pips à {ideal_sl:.{self.digits}f}")


            if ideal_sl == 0 or ideal_tp == 0 or abs(price - ideal_sl) < self.symbol_info.point * 5: # Augmenter un peu la marge min
                self.log.error(f"SL/TP invalide ou trop serré. SL: {ideal_sl}, TP: {ideal_tp}, Prix: {price}. Trade annulé.")
                return 0.0, 0.0, 0.0

            ideal_volume = self._calculate_volume(equity, risk_percent, price, ideal_sl)

            # Vérifier si volume > 0 avant de vérifier contre volume_min
            if ideal_volume <= 0:
                 self.log.warning(f"Le volume idéal calculé est nul ou négatif ({ideal_volume:.4f}). Trade annulé.")
                 return 0.0, 0.0, 0.0

            if ideal_volume < self.symbol_info.volume_min:
                self.log.warning(f"Le volume idéal ({ideal_volume:.4f}) est inférieur au min ({self.symbol_info.volume_min}). Trade annulé.")
                return 0.0, 0.0, 0.0

            return ideal_volume, ideal_sl, ideal_tp

        except Exception as e:
            self.log.error(f"Erreur inattendue lors du calcul des paramètres de trade : {e}", exc_info=True)
            return 0.0, 0.0, 0.0

    def _calculate_volume(self, equity: float, risk_percent: float, entry_price: float, sl_price: float) -> float:
        # --- Fonction inchangée ---
        self.log.debug("--- DÉBUT DU CALCUL DE VOLUME SÉCURISÉ ---")

        risk_amount_account_currency = equity * risk_percent
        self.log.debug(f"1. Capital: {equity:.2f} | Risque: {risk_percent:.2%} -> Montant à risquer: {risk_amount_account_currency:.2f} {self.account_info.currency}")

        sl_distance_price = abs(entry_price - sl_price)
        if sl_distance_price < self.point: # Une distance nulle ou négative n'a pas de sens
            self.log.error("Distance du SL quasi nulle ou négative. Annulation du calcul de volume.")
            return 0.0
        self.log.debug(f"2. Distance SL: {sl_distance_price:.{self.digits}f} (en prix de l'actif)")

        loss_per_lot_profit_currency = sl_distance_price * self.symbol_info.trade_contract_size
        profit_currency = self.symbol_info.currency_profit
        self.log.debug(f"3. Perte/Lot en devise de profit ({profit_currency}): {loss_per_lot_profit_currency:.2f}")

        loss_per_lot_account_currency = loss_per_lot_profit_currency
        if profit_currency != self.account_info.currency:
            conversion_rate = self.get_conversion_rate(profit_currency, self.account_info.currency)
            if not conversion_rate or conversion_rate <= 0:
                self.log.error(f"Impossible d'obtenir un taux de conversion valide pour {profit_currency}->{self.account_info.currency}. Annulation.")
                return 0.0
            loss_per_lot_account_currency *= conversion_rate
            self.log.debug(f"4. Conversion: {profit_currency}/{self.account_info.currency} @ {conversion_rate:.5f} -> Perte/Lot en devise du compte: {loss_per_lot_account_currency:.2f}")
        else:
            self.log.debug("4. Pas de conversion de devise nécessaire.")

        if loss_per_lot_account_currency <= 0:
            self.log.error("La perte par lot calculée est nulle ou négative. Annulation.")
            return 0.0

        volume = risk_amount_account_currency / loss_per_lot_account_currency
        self.log.debug(f"5. Volume brut: {risk_amount_account_currency:.2f} / {loss_per_lot_account_currency:.2f} = {volume:.4f} lots")

        volume_step = self.symbol_info.volume_step
        if volume_step <= 0:
            self.log.warning("Volume step est zéro ou négatif, impossible d'ajuster le volume.")
            # Retourner le volume brut ou 0.0? Retourner 0.0 pour la sécurité.
            return 0.0
        # Ajustement au step le plus proche inférieur
        volume = math.floor(volume / volume_step) * volume_step
        # Arrondir pour éviter les problèmes de précision flottante infimes
        volume = round(volume, 8) # 8 décimales devraient suffire pour les lots

        # S'assurer que le volume final est dans les limites min/max
        # Note: volume_min peut être légèrement supérieur à 0 après ajustement au step si volume brut était très petit
        final_volume = max(self.symbol_info.volume_min, min(self.symbol_info.volume_max, volume))
        # Si le volume ajusté est inférieur au minimum, mais que le volume brut était >= min, forcer au min?
        # Pour l'instant, si l'ajustement le rend < min, il sera filtré plus tard.

        self.log.debug(f"6. Volume final ajusté: {final_volume:.4f} (Min: {self.symbol_info.volume_min}, Max: {self.symbol_info.volume_max}, Step: {volume_step})")
        self.log.debug("--- FIN DU CALCUL DE VOLUME ---")
        return final_volume


    def _calculate_initial_sl_tp(self, price: float, ohlc_data: pd.DataFrame, trade_signal: dict) -> Tuple[float, float]:
         # --- Logique inchangée, mais ajout de la stratégie SMC_STRUCTURE (non implémentée) ---
        rm_settings = self._config.get('risk_management', {})
        sl_strategy = rm_settings.get('sl_strategy', 'ATR_MULTIPLE') # Utiliser sl_strategy de config
        tp_strategy = rm_settings.get('tp_strategy', 'ATR_MULTIPLE') # Utiliser tp_strategy de config
        direction = trade_signal['direction']

        # Calcul ATR (commun aux deux stratégies pour l'instant pour le SL)
        atr_settings_key = trade_signal.get('symbol', 'default') # Utiliser config spécifique symbole si existe
        atr_settings = rm_settings.get('atr_settings', {}).get(atr_settings_key, rm_settings.get('atr_settings', {}).get('default', {}))

        atr = self.calculate_atr(ohlc_data, atr_settings.get('period', 14))
        if atr is None or atr <= 0:
            self.log.error("ATR invalide. Impossible de calculer SL/TP basé sur ATR.")
            return 0.0, 0.0

        # Calcul SL
        sl = 0.0
        if sl_strategy == "ATR_MULTIPLE":
             sl_multiple = atr_settings.get('sl_multiple', 1.5)
             sl_distance = atr * sl_multiple
             sl = price - sl_distance if direction == BUY else price + sl_distance
             self.log.debug(f"SL (ATR): Distance={sl_distance:.{self.digits}f} -> SL={sl:.{self.digits}f}")
        elif sl_strategy == "SMC_STRUCTURE":
             # --- Logique SMC_STRUCTURE SL à implémenter ---
             # Trouver le dernier swing low (pour BUY) ou swing high (pour SELL) pertinent
             # swing_highs, swing_lows = self._find_swing_points(ohlc_data.iloc[-50:], n=3) # Exemple
             # if direction == BUY and not swing_lows.empty:
             #     sl = swing_lows['low'].iloc[-1] # Prendre le dernier swing low
             # elif direction == SELL and not swing_highs.empty:
             #     sl = swing_highs['high'].iloc[-1] # Prendre le dernier swing high
             # else:
             #     self.log.warning("Impossible de trouver la structure SMC pour le SL, fallback sur ATR.")
             #     # Fallback sur ATR pour l'instant
             sl_multiple = atr_settings.get('sl_multiple', 1.5)
             sl_distance = atr * sl_multiple
             sl = price - sl_distance if direction == BUY else price + sl_distance
             self.log.debug(f"SL (SMC Structure - Fallback ATR): Distance={sl_distance:.{self.digits}f} -> SL={sl:.{self.digits}f}")
             # --- Fin logique SMC_STRUCTURE ---
        else:
             self.log.error(f"Stratégie SL '{sl_strategy}' non reconnue.")
             return 0.0, 0.0

        # Calcul TP
        tp = 0.0
        if tp_strategy == "SMC_LIQUIDITY_TARGET":
            tp = trade_signal.get('target_price')
            if not tp:
                self.log.error("Stratégie TP SMC choisie mais aucune 'target_price' trouvée dans le signal.")
                 # Fallback sur ATR TP? Ou annuler? Annuler pour l'instant.
                return 0.0, 0.0
            self.log.debug(f"TP (SMC Target): Cible de liquidité à {tp:.{self.digits}f}")

        elif tp_strategy == "ATR_MULTIPLE":
            tp_multiple = atr_settings.get('tp_multiple', 3.0)
            tp_distance = atr * tp_multiple
            tp = price + tp_distance if direction == BUY else price - tp_distance
            self.log.debug(f"TP (ATR Multiple): Distance={tp_distance:.{self.digits}f} -> TP={tp:.{self.digits}f}")
        else:
            self.log.error(f"Stratégie TP '{tp_strategy}' non reconnue.")
            return 0.0, 0.0

        # Vérification Ratio RR implicite (TP doit être plus loin que SL)
        if sl == 0 or tp == 0: return 0.0, 0.0 # Si l'un est invalide
        if abs(tp - price) < abs(sl - price):
            self.log.warning(f"Le TP ({tp:.{self.digits}f}) est plus proche que le SL ({sl:.{self.digits}f}). Ratio < 1. Vérifiez la configuration.")
            # Ne pas annuler le trade ici, juste prévenir.

        return round(sl, self.digits), round(tp, self.digits)


    def get_conversion_rate(self, from_currency: str, to_currency: str) -> Optional[float]:
        # --- Fonction inchangée ---
        if from_currency == to_currency: return 1.0

        # Essayer paire directe
        pair1 = f"{from_currency}{to_currency}"
        info1 = self._executor._mt5.symbol_info_tick(pair1)
        if info1 and info1.ask > 0:
            self.log.debug(f"Taux de change direct trouvé pour {pair1}: {info1.ask}")
            return info1.ask

        # Essayer paire inverse
        pair2 = f"{to_currency}{from_currency}"
        info2 = self._executor._mt5.symbol_info_tick(pair2)
        if info2 and info2.bid > 0:
            rate = 1.0 / info2.bid
            self.log.debug(f"Taux de change inverse trouvé pour {pair2}: {info2.bid}. Taux calculé: {rate}")
            return rate

        # Essayer via pivot (USD, EUR, GBP)
        for pivot in ["USD", "EUR", "GBP"]:
             if from_currency != pivot and to_currency != pivot:
                 rate1 = self.get_conversion_rate(from_currency, pivot) # Appel récursif
                 rate2 = self.get_conversion_rate(pivot, to_currency) # Appel récursif
                 if rate1 and rate2:
                     cross_rate = rate1 * rate2
                     self.log.debug(f"Taux de change croisé trouvé via {pivot} ({from_currency}->{pivot}@{rate1:.5f}, {pivot}->{to_currency}@{rate2:.5f}): {cross_rate:.5f}")
                     return cross_rate

        self.log.error(f"Impossible de trouver une paire de conversion pour {from_currency} -> {to_currency}")
        return None

    def calculate_atr(self, ohlc_data: pd.DataFrame, period: int) -> Optional[float]:
        # --- Fonction inchangée ---
        if ohlc_data is None or ohlc_data.empty or len(ohlc_data) < period + 1: # Besoin d'au moins P+1 pour le shift
            self.log.warning(f"Pas assez de données pour calculer l'ATR sur {period} périodes (reçu {len(ohlc_data)} barres).")
            return None
        try:
             high_low = ohlc_data['high'] - ohlc_data['low']
             high_close = np.abs(ohlc_data['high'] - ohlc_data['close'].shift())
             low_close = np.abs(ohlc_data['low'] - ohlc_data['close'].shift())

             ranges = pd.concat([high_low, high_close, low_close], axis=1)
             true_range = np.max(ranges, axis=1)
             # Utiliser ewm().mean() pour un ATR exponentiel standard
             atr = true_range.ewm(span=period, adjust=False).mean().iloc[-1]
             if pd.isna(atr) or atr <= 0:
                  self.log.warning(f"Calcul ATR invalide (NaN ou <=0) pour période {period}. ATR={atr}")
                  return None
             return atr
        except Exception as e:
             self.log.error(f"Erreur calcul ATR: {e}", exc_info=True)
             return None

    # --- [Risk-2] Logique TP Partiels ---
    def manage_open_positions(self, positions: list, current_tick, ohlc_data: pd.DataFrame):
        """ Gère BE, Trailing et déclenche les TP partiels. """
        if not positions or not current_tick or ohlc_data is None or ohlc_data.empty: return []

        partial_close_actions = [] # Liste pour retourner les actions de clôture
        risk_settings = self._config.get('risk_management', {})

        # 1. Gérer TP Partiels (avant BE/Trailing pour potentiellement sécuriser plus tôt)
        if risk_settings.get('partial_tp', {}).get('enabled', False):
            partial_tp_config = risk_settings.get('partial_tp', {})
            actions = self._apply_partial_tp(positions, current_tick, partial_tp_config)
            partial_close_actions.extend(actions)

        # 2. Gérer Breakeven
        if risk_settings.get('breakeven', {}).get('enabled', False):
            self._apply_breakeven(positions, current_tick, risk_settings.get('breakeven', {}))

        # 3. Gérer Trailing Stop ATR
        if risk_settings.get('trailing_stop_atr', {}).get('enabled', False):
            self._apply_trailing_stop_atr(positions, current_tick, ohlc_data, risk_settings)

        return partial_close_actions # Retourner les actions demandées


    def _apply_partial_tp(self, positions: list, tick, partial_cfg: dict):
        """Vérifie si des niveaux de TP partiels sont atteints."""
        actions = []
        levels = partial_cfg.get('levels', [])
        if not levels: return actions

        # Trier les niveaux par RR croissant pour les traiter dans l'ordre
        levels.sort(key=lambda x: x.get('rr', 0))

        for pos in positions:
            if pos.sl == 0: continue # Ne peut pas calculer RR sans SL initial valide

            initial_risk_pips = abs(pos.price_open - pos.sl) / self.point
            if initial_risk_pips <= 0: continue

            current_pnl_pips = 0
            if pos.type == mt5.ORDER_TYPE_BUY:
                current_pnl_pips = (tick.bid - pos.price_open) / self.point
            elif pos.type == mt5.ORDER_TYPE_SELL:
                current_pnl_pips = (pos.price_open - tick.ask) / self.point

            current_rr = current_pnl_pips / initial_risk_pips if initial_risk_pips > 0 else 0

            # Initialiser le set pour le ticket si non existant
            if pos.ticket not in self._partial_tp_taken:
                self._partial_tp_taken[pos.ticket] = set()

            taken_levels = self._partial_tp_taken[pos.ticket]

            for level_cfg in levels:
                target_rr = level_cfg.get('rr')
                percentage_to_close = level_cfg.get('percentage') / 100.0 # Convertir en fraction

                # Vérifier si ce niveau RR est configuré, atteint et pas déjà pris
                if target_rr is not None and current_rr >= target_rr and target_rr not in taken_levels:
                    # Trouver le contexte pour obtenir le volume initial
                    order_id = next((order for order, ctx in self._executor._trade_context.items() if ctx.get('position_id') == pos.identifier), None) # Besoin de mapper position_id à order_id ou stocker volume initial différemment
                    
                    initial_volume = self._executor._trade_context.get(order_id, {}).get('volume_initial', pos.volume) # Fallback sur volume actuel si contexte introuvable

                    if initial_volume is None:
                         self.log.warning(f"Impossible de trouver le volume initial pour la position {pos.ticket} (Ordre {order_id}). TP Partiel {target_rr}R ignoré.")
                         continue


                    volume_to_close = initial_volume * percentage_to_close
                    # Ajuster au volume step
                    volume_step = self.symbol_info.volume_step
                    if volume_step > 0:
                         volume_to_close = math.floor(volume_to_close / volume_step) * volume_step
                         volume_to_close = round(volume_to_close, 8)

                    # S'assurer qu'on ne ferme pas plus que le volume restant
                    volume_to_close = min(volume_to_close, pos.volume)

                    if volume_to_close >= self.symbol_info.volume_min:
                        self.log.info(f"TP PARTIEL {target_rr}R atteint pour ticket #{pos.ticket}. Clôture de {volume_to_close:.2f} lots ({level_cfg.get('percentage')}%)")
                        actions.append({
                            'ticket': pos.ticket,
                            'volume': volume_to_close,
                            'trade_id': f"TP{target_rr}R" # Ajouter un identifiant pour le log
                        })
                        taken_levels.add(target_rr) # Marquer ce niveau comme pris

                        # Gérer le déplacement du SL au BE après TP1 si configuré
                        if target_rr == levels[0].get('rr') and partial_cfg.get('move_sl_to_be_after_tp1', False):
                            be_pips = partial_cfg.get('be_pips_plus_after_tp1', 0)
                            breakeven_sl = pos.price_open + (be_pips * self.point) if pos.type == mt5.ORDER_TYPE_BUY else pos.price_open - (be_pips * self.point)
                            # Vérifier si le nouveau SL est meilleur que l'actuel
                            should_move_sl = False
                            if pos.type == mt5.ORDER_TYPE_BUY and breakeven_sl > pos.sl: should_move_sl = True
                            if pos.type == mt5.ORDER_TYPE_SELL and (pos.sl == 0 or breakeven_sl < pos.sl): should_move_sl = True
                            
                            if should_move_sl:
                                 self.log.info(f"Déplacement du SL au BE+{be_pips}pips ({breakeven_sl:.{self.digits}f}) après TP1 pour ticket #{pos.ticket}.")
                                 # Appeler directement modify_position ici car c'est une conséquence du TP partiel
                                 self._executor.modify_position(pos.ticket, breakeven_sl, pos.tp, trade_id=f"BE_after_TP{target_rr}R")


                    else:
                        self.log.warning(f"Volume calculé pour TP Partiel {target_rr}R ({volume_to_close}) est inférieur au minimum ({self.symbol_info.volume_min}).")
                        # Marquer comme pris même si non exécuté pour éviter tentatives répétées? Ou laisser pour plus tard si volume augmente? Laisser pour l'instant.

        return actions


    def _apply_breakeven(self, positions: list, tick, be_cfg: dict):
        # --- Fonction inchangée ---
        trigger_pips = be_cfg.get('trigger_pips', 100) # Valeur par défaut de la config
        pips_plus = be_cfg.get('pips_plus', 10) # Valeur par défaut de la config
        trigger_distance = trigger_pips * self.point
        be_adjustment = pips_plus * self.point

        for pos in positions:
            pnl_pips = 0.0
            move_sl = False
            breakeven_sl = pos.sl # Initialiser avec SL actuel

            if pos.type == mt5.ORDER_TYPE_BUY:
                pnl_distance = tick.bid - pos.price_open
                if pnl_distance >= trigger_distance:
                    potential_be_sl = pos.price_open + be_adjustment
                    # Ne déplacer que si le nouveau SL est meilleur (plus haut) que l'actuel
                    if potential_be_sl > pos.sl:
                         move_sl = True
                         breakeven_sl = potential_be_sl
            elif pos.type == mt5.ORDER_TYPE_SELL:
                pnl_distance = pos.price_open - tick.ask
                if pnl_distance >= trigger_distance:
                    potential_be_sl = pos.price_open - be_adjustment
                     # Ne déplacer que si le nouveau SL est meilleur (plus bas) que l'actuel (ou si SL actuel est 0)
                    if pos.sl == 0 or potential_be_sl < pos.sl:
                         move_sl = True
                         breakeven_sl = potential_be_sl

            if move_sl:
                self.log.info(f"BREAK-EVEN déclenché pour ticket #{pos.ticket}. Nouveau SL: {breakeven_sl:.{self.digits}f} (+{pips_plus} pips)")
                self._executor.modify_position(pos.ticket, breakeven_sl, pos.tp, trade_id="BE")


    def _apply_trailing_stop_atr(self, positions: list, tick, ohlc_data: pd.DataFrame, risk_cfg: dict):
        # --- Fonction inchangée ---
        ts_cfg = risk_cfg.get('trailing_stop_atr', {})
        # Utiliser config ATR spécifique symbole si dispo, sinon défaut
        atr_settings_key = self._symbol # Utiliser le symbole géré par cette instance RM
        atr_cfg = risk_cfg.get('atr_settings', {}).get(atr_settings_key, risk_cfg.get('atr_settings', {}).get('default', {}))

        period = atr_cfg.get('period', 14)
        atr = self.calculate_atr(ohlc_data, period)
        if atr is None or atr <= 0:
            self.log.debug("ATR invalide pour Trailing Stop.")
            return

        activation_multiple = ts_cfg.get('activation_multiple', 2.0)
        trailing_multiple = ts_cfg.get('trailing_multiple', 1.8)
        activation_distance = atr * activation_multiple
        trailing_distance = atr * trailing_multiple

        for pos in positions:
            move_sl = False
            new_sl = pos.sl # Initialiser avec SL actuel

            if pos.type == mt5.ORDER_TYPE_BUY:
                 # Vérifier si le profit atteint le seuil d'activation
                if (tick.bid - pos.price_open) >= activation_distance:
                    potential_new_sl = tick.bid - trailing_distance
                    # Ne déplacer le SL que s'il est meilleur (plus haut) que l'actuel
                    if potential_new_sl > pos.sl:
                        new_sl = potential_new_sl
                        move_sl = True
            elif pos.type == mt5.ORDER_TYPE_SELL:
                 # Vérifier si le profit atteint le seuil d'activation
                if (pos.price_open - tick.ask) >= activation_distance:
                    potential_new_sl = tick.ask + trailing_distance
                     # Ne déplacer le SL que s'il est meilleur (plus bas) que l'actuel (ou si SL actuel = 0)
                    if pos.sl == 0 or potential_new_sl < pos.sl:
                        new_sl = potential_new_sl
                        move_sl = True

            if move_sl:
                rounded_new_sl = round(new_sl, self.digits)
                # Vérifier si le nouveau SL arrondi est effectivement différent de l'ancien
                if rounded_new_sl != round(pos.sl, self.digits):
                     self.log.info(f"TRAILING STOP ATR: Mise à jour SL pour #{pos.ticket} à {rounded_new_sl:.{self.digits}f} (ATR={atr:.{self.digits}f}, Mult={trailing_multiple})")
                     self._executor.modify_position(pos.ticket, rounded_new_sl, pos.tp, trade_id="TS_ATR")

    # --- Helper pour SMC SL (si implémenté) ---
    # def _find_swing_points(self, df: pd.DataFrame, n: int = 2):
    #     df = df.copy() # Travailler sur une copie
    #     df['is_swing_high'] = df['high'].rolling(window=2*n+1, center=True, min_periods=n+1).max() == df['high']
    #     df['is_swing_low'] = df['low'].rolling(window=2*n+1, center=True, min_periods=n+1).min() == df['low']
    #     swing_highs = df[df['is_swing_high']]
    #     swing_lows = df[df['is_swing_low']]
    #     return swing_highs, swing_lows