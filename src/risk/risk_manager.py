# Fichier: src/risk/risk_manager.py
# Version: 19.3.0 (Ajout Filtre R:R Minimum)
# Dépendances: MetaTrader5, pandas, numpy, logging, pytz, src.constants

import MetaTrader5 as mt5
import logging
import math
import pandas as pd
import numpy as np
from datetime import datetime
import pytz
from typing import Tuple, List, Dict, Optional, Any

from src.constants import BUY, SELL, PATTERN_INBALANCE, PATTERN_ORDER_BLOCK

class RiskManager:
    """
    Gère le risque (SMC R7, Overrides R4, Filtre R:R Min).
    v19.3.0: Ajoute filtre R:R minimum.
    """
    def __init__(self, config: dict, executor, symbol: str):
        self.log = logging.getLogger(self.__class__.__name__)
        self._config: Dict = config # Stocké comme _config
        self._executor = executor
        self._symbol: str = symbol

        self.symbol_info_mt5 = self._executor._mt5.symbol_info(self._symbol)
        self.account_info = self._executor._mt5.account_info()

        if not self.symbol_info_mt5 or not self.account_info:
            self.log.critical(f"Infos MT5 indispo pour {self._symbol} ou compte.")
            raise ValueError("Infos MT5 manquantes.")

        self.point: float = self.symbol_info_mt5.point
        self.digits: int = self.symbol_info_mt5.digits

        self.symbol_info: Dict[str, Any] = self._apply_overrides(self.symbol_info_mt5, self._symbol)

    def _apply_overrides(self, mt5_info, symbol: str) -> Dict[str, Any]:
        # ... (Logique inchangée depuis v19.2.1) ...
        overrides = self._config.get('symbol_overrides', {}).get(symbol, {})
        info_dict = {}
        fields_to_copy = [
            'name', 'description', 'currency_base', 'currency_profit', 'currency_margin',
            'trade_contract_size', 'volume_min', 'volume_max', 'volume_step',
            'point', 'digits', 'spread', 'spread_float', 'trade_mode',
            'margin_initial', 'margin_maintenance', 'session_deals',
            'price_change', 'price_volatility', 'price_theoretical'
        ]
        for field in fields_to_copy:
             if hasattr(mt5_info, field): info_dict[field] = getattr(mt5_info, field)
        if not overrides: return info_dict
        applied_list = []
        for config_key, value in overrides.items():
            mt5_key = config_key
            if config_key == 'contract_size': mt5_key = 'trade_contract_size'
            if mt5_key in info_dict:
                try:
                    original_type = type(info_dict[mt5_key])
                    info_dict[mt5_key] = original_type(value)
                    applied_list.append(f"{mt5_key}({config_key})={info_dict[mt5_key]}")
                except Exception as e: self.log.error(f"Erreur override '{config_key}={value}' (->{mt5_key}) pour {symbol}: {e}")
            else: self.log.warning(f"Override ignoré: Attribut '{config_key}' (->'{mt5_key}') non trouvé pour {symbol}.")
        if applied_list: self.log.info(f"Overrides pour {symbol}: {', '.join(applied_list)}")
        return info_dict


    def is_daily_loss_limit_reached(self) -> Tuple[bool, float]:
        # ... (Logique inchangée depuis v19.2.1) ...
        risk_settings=self._config.get('risk_management',{});limit_percent=risk_settings.get('daily_loss_limit_percent',2.0)
        if limit_percent<=0: return False,0.0
        try:
            start_utc=datetime.utcnow().replace(hour=0,minute=0,second=0,microsecond=0)
            deals=self._executor._mt5.history_deals_get(start_utc,datetime.utcnow())
            if deals is None: return False,0.0
            magic=self._config.get('trading_settings',{}).get('magic_number',0)
            pnl=0.0;deals_by_pos={}
            for d in deals:
                if d.magic==magic:
                    if d.position_id not in deals_by_pos: deals_by_pos[d.position_id]=[]
                    deals_by_pos[d.position_id].append(d)
            for pos_id,pos_deals in deals_by_pos.items():
                if any(d.entry in [mt5.DEAL_ENTRY_OUT,mt5.DEAL_ENTRY_INOUT] for d in pos_deals): pnl+=sum(d.profit for d in pos_deals)
            limit_amount=(self.account_info.equity*limit_percent)/100.0
            if pnl<0 and abs(pnl)>=limit_amount: self.log.critical(f"LIMITE PERTE JOUR ({pnl:.2f}) >= {limit_amount:.2f} ({limit_percent}%) ATTEINTE!"); return True,pnl
            return False,pnl
        except Exception as e: self.log.error(f"Erreur check limite perte: {e}",exc_info=True); return False,0.0

    def get_current_total_risk(self, open_positions: list, equity: float) -> float:
        # ... (Logique inchangée depuis v19.2.1) ...
        if not open_positions or equity == 0:
            return 0.0

        total_risk_usd = 0.0
        for pos in open_positions:
            if pos.sl == 0:
                continue # Pas de SL = risque non calculable (ou 0 si BE)

            # Récupérer infos (cache executor)
            symbol_info_mt5 = self._executor._get_symbol_info(pos.symbol)
            if not symbol_info_mt5:
                self.log.warning(f"Risque Global: Infos MT5 indispo pour {pos.symbol}")
                continue
            
            # Appliquer overrides pour ce symbole
            info_dict = self._apply_overrides(symbol_info_mt5, pos.symbol)
            
            cs = info_dict.get('trade_contract_size', 0)
            pc = info_dict.get('currency_profit', '')
            if cs == 0 or not pc:
                self.log.error(f"Risque Global: Données critiques (CS/PC) manquantes pour {pos.symbol}")
                continue

            loss_points = abs(pos.price_open - pos.sl)
            loss_profit_ccy = loss_points * cs * pos.volume
            
            rate = self.get_conversion_rate(pc, self.account_info.currency)
            if not rate or rate <= 0:
                 self.log.error(f"Risque Global: Taux invalide {pc}->{self.account_info.currency}"); continue
            
            total_risk_usd += (loss_profit_ccy * rate)

        return (total_risk_usd / equity) if equity > 0 else 0.0

    def calculate_trade_parameters(self, equity: float, current_tick, ohlc_data: pd.DataFrame, trade_signal: dict) -> Tuple[float, float, float, float]:
        """Calcule les paramètres de trade (Vol, Entrée Limite, SL, TP) avec filtre R:R."""
        try:
            req_keys=['direction','entry_zone_start','entry_zone_end','stop_loss_level','target_price']
            if not isinstance(trade_signal,dict) or not all(k in trade_signal for k in req_keys):
                self.log.error(f"Signal SMC invalide (clés manquantes): {trade_signal}")
                return 0.0, 0.0, 0.0, 0.0

            direction, pattern = trade_signal['direction'], trade_signal['pattern']
            z_start, z_end = trade_signal['entry_zone_start'], trade_signal['entry_zone_end']
            sl_struct, tp_target = trade_signal['stop_loss_level'], trade_signal['target_price']

            entry_limit = self._calculate_limit_entry_price(z_start, z_end, pattern, direction)
            sl_final = self._calculate_final_sl(sl_struct, direction, ohlc_data)
            tp_final = round(tp_target, self.digits)

            # Vérifications initiales
            if entry_limit == 0.0 or sl_final == 0.0 or tp_final == 0.0:
                self.log.warning(f"Calcul paramètres échoué (Entrée/SL/TP nul): E={entry_limit:.{self.digits}f}, SL={sl_final:.{self.digits}f}, TP={tp_final:.{self.digits}f}")
                return 0.0, 0.0, 0.0, 0.0

            # Vérification retracement manqué
            current_price = current_tick.ask if direction == BUY else current_tick.bid
            if (direction == BUY and current_price < entry_limit) or \
               (direction == SELL and current_price > entry_limit):
                self.log.warning(f"Retracement manqué {self._symbol}. Actuel={current_price:.{self.digits}f}, Limite={entry_limit:.{self.digits}f}.")
                return 0.0, 0.0, 0.0, 0.0

            # Vérifications SL/TP vs Entrée
            min_sl_dist = self.point * 5 # Ex: 5 points minimum
            sl_distance = abs(entry_limit - sl_final)
            tp_distance = abs(tp_final - entry_limit)

            if sl_distance < min_sl_dist:
                self.log.error(f"SL trop proche de l'entrée ({sl_distance/self.point:.1f} points < {min_sl_dist/self.point:.1f}). SL={sl_final:.{self.digits}f}, Limite={entry_limit:.{self.digits}f}. Annulé.")
                return 0.0, 0.0, 0.0, 0.0

            if tp_distance < self.point: # TP doit être au moins à 1 point de l'entrée
                self.log.error(f"TP trop proche ou invalide vs Entrée. TP={tp_final:.{self.digits}f}, Limite={entry_limit:.{self.digits}f}. Annulé.")
                return 0.0, 0.0, 0.0, 0.0

            # --- NOUVEAU: Vérification R:R Minimum ---
            min_rr = self._config.get('risk_management', {}).get('min_required_rr', 2.0) # Défaut 2.0
            if min_rr > 0 and sl_distance > 0: # Éviter division par zéro
                calculated_rr = tp_distance / sl_distance
                if calculated_rr < min_rr:
                    self.log.warning(f"R:R insuffisant ({calculated_rr:.2f} < {min_rr:.2f}). Entrée={entry_limit:.{self.digits}f}, SL={sl_final:.{self.digits}f}, TP={tp_final:.{self.digits}f}. Annulé.")
                    return 0.0, 0.0, 0.0, 0.0
                else:
                     self.log.debug(f"R:R Vérifié OK ({calculated_rr:.2f} >= {min_rr:.2f})")
            # --- FIN Vérification R:R ---

            # Calcul Volume
            risk_pct = self._config.get('risk_management', {}).get('risk_per_trade', 0.01)
            volume = self._calculate_volume(equity, risk_pct, entry_limit, sl_final)

            if volume < self.symbol_info_mt5.volume_min:
                if volume > 0: # Log uniquement si volume calculé > 0 mais < min MT5
                    self.log.warning(f"Volume calculé ({volume:.4f}) < Min MT5 ({self.symbol_info_mt5.volume_min}). Annulé.")
                # Si volume calculé était déjà 0 (ex: risque trop petit vs min SL dist), pas besoin de log warning
                return 0.0, 0.0, 0.0, 0.0

            # Si tout est OK
            self.log.info(f"Params Ordre Limite OK: {direction} {volume:.2f} @ {entry_limit:.{self.digits}f}, SL={sl_final:.{self.digits}f}, TP={tp_final:.{self.digits}f} (R:R={calculated_rr:.2f})")
            return volume, entry_limit, sl_final, tp_final

        except Exception as e:
            self.log.error(f"Erreur calcul params limite : {e}", exc_info=True)
            return 0.0, 0.0, 0.0, 0.0

    def _calculate_limit_entry_price(self, z_start, z_end, pattern, direction) -> float:
        # ... (Logique inchangée depuis v19.2.1) ...
        cfg = self._config.get('pattern_detection', {}).get('entry_logic', {})
        level = 0.5 # Défaut milieu
        try:
            if pattern == PATTERN_INBALANCE:
                 level = cfg.get('fvg_entry_level', 0.5) # Défaut 0.5
            elif pattern == PATTERN_ORDER_BLOCK:
                 level = cfg.get('ob_entry_level', 0.5) # (J.1) Défaut 0.5

            zone_min, zone_max = min(z_start, z_end), max(z_start, z_end)
            zone_size = zone_max - zone_min
            
            # Gérer le cas où la zone est de taille nulle (peut arriver?)
            if zone_size <= 0:
                 self.log.warning(f"Calcul entrée: Zone size nulle ou négative ({zone_size}). Utilise milieu ({zone_min}).")
                 return round(zone_min, self.digits) # Retourne une des bornes

            entry_price = zone_max - (zone_size * level) if direction == BUY else zone_min + (zone_size * level)

            return round(entry_price, self.digits)
        except Exception as e:
            self.log.error(f"Erreur calcul entrée limite {pattern}: {e}")
            return 0.0


    def _calculate_final_sl(self, sl_structural, direction, ohlc_data: pd.DataFrame) -> float:
        # ... (Logique inchangée depuis v19.2.1) ...
        rm_cfg = self._config.get('risk_management',{})
        buffer_atr_multi = rm_cfg.get('sl_buffer_atr_multiple', 0.0)
        buffer = 0.0

        if buffer_atr_multi > 0.0:
            atr_cfg = rm_cfg.get('atr_settings', {}).get('default', {})
            atr_period = atr_cfg.get('period', 14)
            atr = self.calculate_atr(ohlc_data, atr_period)
            if atr and atr > 0:
                buffer = atr * buffer_atr_multi
                self.log.debug(f"Buffer SL (ATR): {buffer_atr_multi} * {atr:.5f} = {buffer:.5f}")
            else:
                buffer_pips = rm_cfg.get('sl_buffer_pips', 0) # Fallback Pips
                buffer = buffer_pips * self.point
                self.log.warning(f"Buffer SL (ATR) échec, fallback pips: {buffer_pips}p = {buffer:.5f}")
        else:
            buffer_pips = rm_cfg.get('sl_buffer_pips', 0) # Utilise Pips si ATR non configuré
            buffer = buffer_pips * self.point
            if buffer > 0: self.log.debug(f"Buffer SL (Pips): {buffer_pips}p = {buffer:.5f}")
            else: self.log.debug("Buffer SL (Pips): 0")

        # Vérifier que le buffer n'inverse pas le SL (ex: SL structurel très proche de l'entrée)
        sl = sl_structural - buffer if direction == BUY else sl_structural + buffer
        
        # S'assurer que le SL final est bien du côté protecteur par rapport au structurel
        if direction == BUY and sl > sl_structural:
            self.log.warning(f"Buffer SL a inversé le SL Buy (Final {sl} > Struct {sl_structural}). Utilise structurel.")
            sl = sl_structural
        elif direction == SELL and sl < sl_structural:
            self.log.warning(f"Buffer SL a inversé le SL Sell (Final {sl} < Struct {sl_structural}). Utilise structurel.")
            sl = sl_structural
            
        return round(sl, self.digits)


    def _calculate_volume(self, equity: float, risk_pct: float, entry: float, sl: float) -> float:
        # ... (Logique inchangée depuis v19.2.1) ...
        self.log.debug("--- Calcul Volume (Overrides actifs via dict) ---")
        try:
            # Utiliser self.symbol_info qui contient les overrides
            cs = self.symbol_info.get('trade_contract_size', 0)
            pc = self.symbol_info.get('currency_profit', '')
            # Utiliser self.symbol_info_mt5 pour les limites/step broker
            vol_min = self.symbol_info_mt5.volume_min
            vol_max = self.symbol_info_mt5.volume_max
            step = self.symbol_info_mt5.volume_step

            if cs == 0 or not pc:
                self.log.error(f"Données critiques (CS={cs}, PC='{pc}') manquantes dans symbol_info pour {self._symbol}")
                return 0.0
            self.log.debug(f"Compte: Eq={equity:.2f} {self.account_info.currency}");
            self.log.debug(f"Symbole ({self._symbol}): CS={cs}, ProfitCcy={pc}, MinV={vol_min}, MaxV={vol_max}, Step={step}")

        except Exception as e:
            self.log.warning(f"Erreur lecture infos symbole/compte pour calcul volume: {e}")
            return 0.0

        risk_amt = equity * risk_pct
        self.log.debug(f"1. Risque Max: {risk_amt:.2f} {self.account_info.currency} ({risk_pct*100:.2f}%)")

        sl_dist = abs(entry - sl)
        if sl_dist < self.point: # Vérifier distance SL minimale (1 point)
            self.log.error(f"Distance SL ({sl_dist:.{self.digits}f}) trop faible ou nulle. Entrée={entry:.{self.digits}f}, SL={sl:.{self.digits}f}")
            return 0.0
        self.log.debug(f"2. Distance SL: {sl_dist:.{self.digits}f} ({sl_dist/self.point:.1f} points)")

        loss_per_lot_profit_ccy = sl_dist * cs
        self.log.debug(f"3. Perte/Lot ({pc}): {loss_per_lot_profit_ccy:.2f}")

        loss_per_lot_account_ccy = loss_per_lot_profit_ccy
        if pc != self.account_info.currency:
            rate = self.get_conversion_rate(pc, self.account_info.currency)
            if not rate or rate <= 0:
                self.log.error(f"Taux de conversion invalide ou introuvable: {pc} -> {self.account_info.currency}")
                return 0.0
            loss_per_lot_account_ccy *= rate
            self.log.debug(f"4. Taux Conv ({pc}->{self.account_info.currency}): {rate:.5f} -> Perte/Lot ({self.account_info.currency}): {loss_per_lot_account_ccy:.2f}")
        else:
            self.log.debug(f"4. Pas de Conv. nécessaire ({pc} == {self.account_info.currency})")

        if loss_per_lot_account_ccy <= 0:
            self.log.error(f"Perte par lot calculée nulle ou négative ({loss_per_lot_account_ccy:.2f}). Vérifier CS/Taux.")
            return 0.0

        # Volume brut
        volume_raw = risk_amt / loss_per_lot_account_ccy
        self.log.debug(f"5. Volume brut calculé: {volume_raw:.4f} lots")

        # Ajustement au step broker
        if step > 0:
            volume_adjusted = math.floor(volume_raw / step) * step
        else:
            self.log.warning(f"Volume step invalide ({step}) pour {self._symbol}. Utilise volume brut.")
            volume_adjusted = volume_raw # Fallback prudent

        # Application des limites min/max broker
        final_volume = max(vol_min, min(vol_max, volume_adjusted))

        if final_volume != volume_adjusted:
             if final_volume == vol_min and volume_adjusted < vol_min:
                  self.log.debug(f"Volume ajusté ({volume_adjusted:.4f}) < Min Broker ({vol_min:.4f}). Augmenté à {final_volume:.4f}.")
             elif final_volume == vol_max and volume_adjusted > vol_max:
                  self.log.warning(f"Volume ajusté ({volume_adjusted:.4f}) > Max Broker ({vol_max:.4f}). Réduit à {final_volume:.4f}.")

        # Vérification finale (le volume final doit être >= min OU == 0)
        if final_volume < vol_min and final_volume > 0:
             self.log.warning(f"Volume final ({final_volume:.4f}) < Min Broker ({vol_min:.4f}) après ajustements. Volume mis à 0.")
             return 0.0 # Ne pas trader si on ne peut pas respecter le min volume

        self.log.debug(f"6. Volume final: {final_volume:.4f} lots")
        return final_volume


    def get_conversion_rate(self, from_ccy: str, to_ccy: str) -> Optional[float]:
        # ... (Logique inchangée depuis v19.2.1) ...
        if from_ccy==to_ccy: return 1.0
        # Tenter la paire directe
        pair1=f"{from_ccy}{to_ccy}"; tick1=self._executor._mt5.symbol_info_tick(pair1)
        if tick1 and tick1.ask > 0: return tick1.ask
        # Tenter la paire inverse
        pair2=f"{to_ccy}{from_ccy}"; tick2=self._executor._mt5.symbol_info_tick(pair2)
        if tick2 and tick2.bid > 0: return 1.0 / tick2.bid
        # Tenter via un pivot (USD, EUR, GBP...)
        for pivot in ["USD", "EUR", "GBP", "JPY", "CHF"]: # Liste étendue
            if from_ccy != pivot and to_ccy != pivot:
                rate1 = self.get_conversion_rate(from_ccy, pivot)
                rate2 = self.get_conversion_rate(pivot, to_ccy)
                if rate1 and rate2 and rate1 > 0 and rate2 > 0:
                    self.log.debug(f"Conversion via {pivot}: {from_ccy}->{pivot}={rate1:.5f}, {pivot}->{to_ccy}={rate2:.5f}")
                    return rate1 * rate2
        self.log.error(f"Taux de conversion introuvable pour {from_ccy} -> {to_ccy}")
        return None

    def calculate_atr(self, ohlc: pd.DataFrame, period: int) -> Optional[float]:
        # ... (Logique inchangée depuis v19.2.1) ...
        if ohlc is None or len(ohlc) < period + 1:
            self.log.debug(f"Pas assez de données pour ATR({period}). Barres disponibles: {len(ohlc) if ohlc is not None else 0}")
            return None
        try:
            # Calcul du True Range (TR)
            high_low = ohlc['high'] - ohlc['low']
            high_close = abs(ohlc['high'] - ohlc['close'].shift())
            low_close = abs(ohlc['low'] - ohlc['close'].shift())
            # Utiliser fillna(0) pour la première ligne où shift() donne NaN
            tr = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1).fillna(0)
            
            # Calcul de l'ATR avec EWMA
            # Utiliser com = period - 1 pour correspondre à la formule standard ATR (alpha = 1/period)
            # atr = tr.ewm(com=period - 1, adjust=False).mean().iloc[-1]
            # Ou utiliser span=period (alpha = 2/(span+1)) - souvent utilisé par défaut dans les libs
            atr = tr.ewm(span=period, adjust=False).mean().iloc[-1]
            
            return atr if pd.notna(atr) and atr > 0 else None
        except Exception as e:
            self.log.warning(f"Erreur calcul ATR (période {period}, barres {len(ohlc)}): {e}")
            return None

    def manage_open_positions(self, positions: list, tick, ohlc: pd.DataFrame, context: dict):
        # ... (Logique inchangée depuis v19.2.1) ...
        if not positions or not tick or ohlc is None or ohlc.empty: return
        cfg = self._config.get('risk_management', {})
        
        atr_cfg = cfg.get('atr_settings', {}).get('default', {})
        atr_period = atr_cfg.get('period', 14)
        atr_m15 = self.calculate_atr(ohlc, atr_period)
        
        if cfg.get('partial_tp', {}).get('enabled', False): self._apply_partial_tp(positions, tick, context, cfg.get('partial_tp', {}))
        if cfg.get('breakeven', {}).get('enabled', False): self._apply_breakeven(positions, tick, context, cfg.get('breakeven', {}), atr_m15)
        if cfg.get('trailing_stop_atr', {}).get('enabled', False): self._apply_trailing_stop_atr(positions, tick, atr_m15, cfg)

    def _apply_partial_tp(self, positions: list, tick, trade_context: dict, partial_tp_cfg: dict):
        # ... (Logique inchangée depuis v19.2.1) ...
        levels = sorted(partial_tp_cfg.get('levels', []), key=lambda x: x.get('rr', 0))
        magic_number = self._config.get('trading_settings', {}).get('magic_number', 0)
        if not levels: return
        for pos in positions:
            # Utiliser pos.ticket comme clé pour le contexte
            ctx = trade_context.get(pos.ticket)
            if not ctx: self.log.debug(f"TP Partiel: Ctx #{pos.ticket} introuvable."); continue
            
            original_sl = ctx.get('original_sl')
            original_volume = ctx.get('original_volume')
            closed_pct = ctx.get('partial_tp_taken_percent', 0.0)

            if not original_sl or not original_volume or original_sl==0 or original_volume==0 or closed_pct >= 0.999: continue
            sl_dist = abs(pos.price_open - original_sl)
            if sl_dist < self.point: continue
            current_price = tick.bid if pos.type == BUY else tick.ask
            profit_dist = (current_price - pos.price_open) if pos.type == BUY else (pos.price_open - current_price)
            if profit_dist <= 0: continue
            current_rr = profit_dist / sl_dist
            target_lvl = None
            for lvl in reversed(levels):
                rr_target, pct_target_cumul = lvl.get('rr', 0), lvl.get('percent', 0) / 100.0 # 'percent' est cumulatif ici
                
                # Trouver le premier niveau RR atteint dont le % cumulatif n'est pas encore totalement pris
                if current_rr >= rr_target and closed_pct < (pct_target_cumul - 0.001): # Tolérance float
                    target_lvl = lvl;
                    break # On prend ce niveau
            
            if not target_lvl: continue
            
            # Calculer le % à prendre MAINTENANT
            total_pct_to_close_cumul = target_lvl.get('percent', 0) / 100.0
            pct_to_take_now = total_pct_to_close_cumul - closed_pct
            
            if pct_to_take_now <= 0.001: continue # Si déjà pris ou erreur float
            
            vol_close = original_volume * pct_to_take_now
            rr_lbl = target_lvl.get('rr')
            self.log.info(f"TP PARTIEL (R{rr_lbl}) #{pos.ticket} (RR:{current_rr:.2f}). Clôture {pct_to_take_now*100:.1f}% ({vol_close:.4f} lots). Total clôturé: {total_pct_to_close_cumul*100:.1f}%.")
            
            res = self._executor.close_partial_position(pos.ticket, vol_close, magic_number, f"Partial TP R{rr_lbl}")
            
            if res:
                # Mettre à jour le contexte avec le nouveau pourcentage total clôturé
                self._executor.update_trade_context_partials(pos.ticket, pct_to_take_now) # La fonction ajoute au total existant
                
                # Gestion BE après TP1 (premier niveau de la liste)
                is_tp1 = (target_lvl.get('rr') == levels[0].get('rr'))
                move_sl_cfg = partial_tp_cfg.get('move_sl_to_be_after_tp1', True)
                be_pips_cfg = partial_tp_cfg.get('be_pips_plus_after_tp1', 5) # Ce paramètre est maintenant obsolète avec BE ATR (J.7) mais gardé pour compatibilité
                
                if is_tp1 and move_sl_cfg:
                     # On pourrait utiliser la logique BE ATR ici, mais la config actuelle utilise pips fixes
                     be_sl = pos.price_open + (be_pips_cfg * self.point) if pos.type == BUY else pos.price_open - (be_pips_cfg * self.point)
                     # Vérifier si ce nouveau SL est meilleur que l'actuel
                     is_better = (pos.type == BUY and be_sl > pos.sl) or \
                                 (pos.type == SELL and (pos.sl == 0 or be_sl < pos.sl))
                     if is_better:
                         self.log.info(f"SL->BE+ ({be_sl:.{self.digits}f}) après TP1 #{pos.ticket}")
                         self._executor.modify_position(pos.ticket, be_sl, pos.tp)


    def _apply_breakeven(self, positions: list, tick, trade_context: dict, cfg: dict, atr_m15: Optional[float]):
        # ... (Logique inchangée depuis v19.2.1) ...
        trig_rr = cfg.get('breakeven_trigger_rr', 0.0)
        trig_pips_fallback = cfg.get('trigger_pips', 0)
        plus_atr_multi = cfg.get('breakeven_plus_atr_multiple', 0.0)
        plus_pips_fallback = cfg.get('pips_plus', 0)

        for pos in positions:
            pnl_pips, be_sl, profit_dist = 0.0, 0.0, 0.0
            current_price = 0.0
            
            if pos.type == BUY:
                current_price = tick.bid
                profit_dist = (current_price - pos.price_open)
                pnl_pips = profit_dist / self.point
                if pos.sl >= pos.price_open: continue # Déjà à BE ou mieux
            elif pos.type == SELL:
                current_price = tick.ask
                profit_dist = (pos.price_open - current_price)
                pnl_pips = profit_dist / self.point
                if pos.sl != 0 and pos.sl <= pos.price_open: continue # Déjà à BE ou mieux

            if profit_dist <= 0: continue # Pas en profit

            # 1. Vérifier condition de déclenchement
            trigger_condition_met = False
            if trig_rr > 0.0:
                ctx = trade_context.get(pos.ticket)
                original_sl = ctx.get('original_sl') if ctx else None
                if original_sl and original_sl != 0:
                    sl_dist = abs(pos.price_open - original_sl)
                    if sl_dist > self.point:
                        current_rr = profit_dist / sl_dist
                        if current_rr >= trig_rr: trigger_condition_met = True
            
            if not trigger_condition_met and trig_pips_fallback > 0: # Fallback Pips
                 if pnl_pips >= trig_pips_fallback: trigger_condition_met = True

            if not trigger_condition_met:
                continue

            # 2. Calculer le nouveau SL (BE + Buffer)
            buffer = 0.0
            if plus_atr_multi > 0.0 and atr_m15 and atr_m15 > 0:
                buffer = atr_m15 * plus_atr_multi
            else:
                buffer = plus_pips_fallback * self.point # Fallback Pips

            if pos.type == BUY:
                be_sl = pos.price_open + buffer
                if be_sl > pos.sl: # Vérifier amélioration
                    self.log.info(f"BE (RR/ATR) #{pos.ticket}. SL-> {be_sl:.{self.digits}f}"); self._executor.modify_position(pos.ticket, be_sl, pos.tp)
            elif pos.type == SELL:
                be_sl = pos.price_open - buffer
                if (pos.sl == 0 or be_sl < pos.sl): # Vérifier amélioration
                    self.log.info(f"BE (RR/ATR) #{pos.ticket}. SL-> {be_sl:.{self.digits}f}"); self._executor.modify_position(pos.ticket, be_sl, pos.tp)

    def _apply_trailing_stop_atr(self, positions: list, tick, atr_m15: Optional[float], cfg: dict):
        # ... (Logique inchangée depuis v19.2.1) ...
        ts_cfg = cfg.get('trailing_stop_atr', {})
        if atr_m15 is None or atr_m15 <= 0: 
            self.log.debug("TS ATR ignoré: ATR M15 invalide.")
            return
            
        act_multi, trail_multi = ts_cfg.get('activation_multiple', 2.0), ts_cfg.get('trailing_multiple', 1.8)
        
        for pos in positions:
            # S'assurer que pos.sl n'est pas None (même si 0.0 est valide)
            current_sl = pos.sl if pos.sl is not None else 0.0
            new_sl = current_sl # Initialiser avec SL actuel
            profit = 0.0

            if pos.type == BUY:
                profit = tick.bid - pos.price_open
                if profit >= (atr_m15 * act_multi):
                    potential_sl = tick.bid - (atr_m15 * trail_multi)
                    # Déplacer seulement si potential_sl est meilleur (plus haut) que le SL actuel
                    if potential_sl > current_sl:
                        new_sl = potential_sl
            elif pos.type == SELL:
                profit = pos.price_open - tick.ask
                if profit >= (atr_m15 * act_multi):
                    potential_sl = tick.ask + (atr_m15 * trail_multi)
                    # Déplacer seulement si potential_sl est meilleur (plus bas) que le SL actuel
                    # Gérer le cas où SL initial est 0.0
                    if current_sl == 0.0 or potential_sl < current_sl:
                        new_sl = potential_sl

            # Appliquer la modification seulement si new_sl a changé
            if new_sl != current_sl:
                 # Arrondir avant d'envoyer
                new_sl_rounded = round(new_sl, self.digits)
                # Vérifier à nouveau après arrondi (peut redevenir égal au SL actuel)
                if new_sl_rounded != round(current_sl, self.digits):
                    self.log.info(f"TS ATR: SL #{pos.ticket} -> {new_sl_rounded:.{self.digits}f} (Profit Pips: {profit/self.point:.1f}, ATR: {atr_m15:.5f})")
