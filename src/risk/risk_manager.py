# Fichier: src/risk/risk_manager.py
# Version: 19.2.1 (Fix J.7 - Logic TS)
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
    Gère le risque (SMC R7, Overrides R4).
    v19.2.1: (J.7) Corrige logique Trailing Stop (SELL).
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
        """(J.4) Applique overrides (basé sur symbol), gère mapping, retourne DICTIONNAIRE."""
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
        # ... (Logique inchangée) ...
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

    # --- (J.4) Calcul Risque Global ---
    def get_current_total_risk(self, open_positions: list, equity: float) -> float:
        """Calcule le risque total (% equity) de toutes les positions ouvertes."""
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
    # --- Fin J.4 ---

    def calculate_trade_parameters(self, equity: float, current_tick, ohlc_data: pd.DataFrame, trade_signal: dict) -> Tuple[float, float, float, float]:
        # ... (Logique inchangée) ...
        try:
            req_keys=['direction','entry_zone_start','entry_zone_end','stop_loss_level','target_price']
            if not isinstance(trade_signal,dict) or not all(k in trade_signal for k in req_keys): self.log.error(f"Signal SMC invalide: {trade_signal}"); return 0.0,0.0,0.0,0.0
            direction,pattern=trade_signal['direction'],trade_signal['pattern']
            z_start,z_end=trade_signal['entry_zone_start'],trade_signal['entry_zone_end']
            sl_struct,tp_target=trade_signal['stop_loss_level'],trade_signal['target_price']
            
            entry_limit=self._calculate_limit_entry_price(z_start,z_end,pattern,direction)
            # (J.5) Passer ohlc_data M15 pour calcul buffer ATR
            sl_final=self._calculate_final_sl(sl_struct, direction, ohlc_data) 
            tp_final=round(tp_target,self.digits)

            if entry_limit==0.0 or sl_final==0.0: return 0.0,0.0,0.0,0.0
            current_price=current_tick.ask if direction==BUY else current_tick.bid
            if (direction==BUY and current_price<entry_limit) or (direction==SELL and current_price>entry_limit): self.log.warning(f"Retracement manqué {self._symbol}. Actuel={current_price:.5f}, Limite={entry_limit:.5f}."); return 0.0,0.0,0.0,0.0
            min_sl_dist=self.point*5
            if abs(entry_limit-sl_final)<min_sl_dist: self.log.error(f"SL trop proche. SL={sl_final:.5f}, Limite={entry_limit:.5f}. Annulé."); return 0.0,0.0,0.0,0.0
            if (direction==BUY and tp_final<=entry_limit) or (direction==SELL and tp_final>=entry_limit): self.log.error(f"TP invalide vs Limite. TP={tp_final:.5f}, Limite={entry_limit:.5f}. Annulé."); return 0.0,0.0,0.0,0.0
            
            risk_pct=self._config.get('risk_management',{}).get('risk_per_trade',0.01)
            volume=self._calculate_volume(equity,risk_pct,entry_limit,sl_final)
            if volume < self.symbol_info_mt5.volume_min: 
                if volume > 0: self.log.warning(f"Volume ({volume:.4f}) < Min MT5 ({self.symbol_info_mt5.volume_min}). Annulé.")
                return 0.0,0.0,0.0,0.0

            self.log.info(f"Params Ordre Limite OK: {direction} {volume:.2f} @ {entry_limit:.5f}, SL={sl_final:.5f}, TP={tp_final:.5f}")
            return volume,entry_limit,sl_final,tp_final
        except Exception as e: self.log.error(f"Erreur calcul params limite : {e}",exc_info=True); return 0.0,0.0,0.0,0.0

    def _calculate_limit_entry_price(self, z_start, z_end, pattern, direction) -> float:
        """(J.1) Calcule prix entrée limite basé sur config (Défaut 0.5 OB)."""
        cfg = self._config.get('pattern_detection', {}).get('entry_logic', {})
        level = 0.5 # Défaut milieu
        try:
            if pattern == PATTERN_INBALANCE:
                 level = cfg.get('fvg_entry_level', 0.5) # Défaut 0.5
            elif pattern == PATTERN_ORDER_BLOCK:
                 level = cfg.get('ob_entry_level', 0.5) # (J.1) Défaut 0.5

            zone_min, zone_max = min(z_start, z_end), max(z_start, z_end)
            zone_size = zone_max - zone_min

            entry_price = zone_max - (zone_size * level) if direction == BUY else zone_min + (zone_size * level)

            return round(entry_price, self.digits)
        except Exception as e:
            self.log.error(f"Erreur calcul entrée limite {pattern}: {e}")
            return 0.0

    def _calculate_final_sl(self, sl_structural, direction, ohlc_data: pd.DataFrame) -> float:
        """(J.5) Calcule SL final basé sur structure + buffer (ATR ou Pips)."""
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
            buffer_pips = rm_cfg.get('sl_buffer_pips', 0) # Utilise Pips
            buffer = buffer_pips * self.point
            self.log.debug(f"Buffer SL (Pips): {buffer_pips}p = {buffer:.5f}")
        
        sl = sl_structural - buffer if direction == BUY else sl_structural + buffer
        return round(sl, self.digits)

    def _calculate_volume(self, equity: float, risk_pct: float, entry: float, sl: float) -> float:
        # ... (Logique inchangée) ...
        self.log.debug("--- Calcul Volume (R4/R9 Overrides actifs via dict) ---")
        try:
            lev = self.account_info.leverage; cs = self.symbol_info.get('trade_contract_size',0); pc = self.symbol_info.get('currency_profit','')
            if cs==0 or not pc: self.log.error(f"Données critiques manquantes dict symbol_info {self._symbol}"); return 0.0
            self.log.debug(f"Compte: Eq={equity:.2f} {self.account_info.currency}, Lev={lev}:1"); self.log.debug(f"Symbole ({self._symbol}): CS={cs}, ProfitCcy={pc}")
        except Exception as e: self.log.warning(f"Erreur log debug R4/R9: {e}"); return 0.0
        risk_amt = equity*risk_pct; self.log.debug(f"1. Risque: {risk_amt:.2f} {self.account_info.currency}")
        sl_dist = abs(entry-sl)
        if sl_dist<self.point: self.log.error("Dist SL nulle"); return 0.0
        self.log.debug(f"2. Dist SL: {sl_dist:.{self.digits}f}")
        loss_lot_profit = sl_dist*cs; self.log.debug(f"3. Perte/Lot ({pc}): {loss_lot_profit:.2f}")
        loss_lot_acc = loss_lot_profit
        if pc!=self.account_info.currency:
            rate = self.get_conversion_rate(pc,self.account_info.currency)
            if not rate or rate<=0: self.log.error(f"Taux invalide {pc}->{self.account_info.currency}"); return 0.0
            loss_lot_acc *= rate; self.log.debug(f"4. Conv @ {rate:.5f} -> Perte/Lot ({self.account_info.currency}): {loss_lot_acc:.2f}")
        else: self.log.debug("4. Pas Conv")
        if loss_lot_acc<=0: self.log.error("Perte/Lot <= 0"); return 0.0
        vol = risk_amt/loss_lot_acc; self.log.debug(f"5. Vol brut: {vol:.4f}")
        step = self.symbol_info_mt5.volume_step;
        if step>0: vol = math.floor(vol/step)*step
        else: self.log.warning("Step MT5 = 0"); return 0.0
        vol_min,vol_max = self.symbol_info_mt5.volume_min, self.symbol_info_mt5.volume_max
        final_vol = max(vol_min,min(vol_max,vol))
        if final_vol<vol_min:
             return 0.0
        self.log.debug(f"6. Vol final: {final_vol:.4f} (Min:{vol_min}, Max:{vol_max}, Step:{step})")
        return final_vol

    def get_conversion_rate(self, from_ccy: str, to_ccy: str) -> Optional[float]:
        # ... (Logique inchangée) ...
        if from_ccy==to_ccy: return 1.0
        p1=f"{from_ccy}{to_ccy}"; i1=self._executor._mt5.symbol_info_tick(p1)
        if i1 and i1.ask>0: return i1.ask
        p2=f"{to_ccy}{from_ccy}"; i2=self._executor._mt5.symbol_info_tick(p2)
        if i2 and i2.bid>0: return 1.0/i2.bid
        for pivot in ["USD","EUR","GBP"]:
            if from_ccy!=pivot and to_ccy!=pivot:
                r1=self.get_conversion_rate(from_ccy,pivot); r2=self.get_conversion_rate(pivot,to_ccy)
                if r1 and r2: return r1*r2
        self.log.error(f"Taux conv introuvable {from_ccy}->{to_ccy}"); return None

    def calculate_atr(self, ohlc: pd.DataFrame, period: int) -> Optional[float]:
        # ... (Logique inchangée) ...
        if ohlc is None or len(ohlc)<period+1: return None
        try:
            hl=ohlc['high']-ohlc['low']; hc=abs(ohlc['high']-ohlc['close'].shift()); lc=abs(ohlc['low']-ohlc['close'].shift())
            tr=pd.concat([hl,hc,lc],axis=1).max(axis=1)
            atr = tr.ewm(span=period,adjust=False).mean().iloc[-1]
            return atr if pd.notna(atr) else None
        except Exception as e:
            self.log.warning(f"Erreur calcul ATR (période {period}, barres {len(ohlc)}): {e}")
            return None

    def manage_open_positions(self, positions: list, tick, ohlc: pd.DataFrame, context: dict):
        """ (J.7) Gère BE/Trailing (ohlc (M15) requis)."""
        if not positions or not tick or ohlc is None or ohlc.empty: return
        cfg = self._config.get('risk_management', {})
        
        # (J.7) Calcul ATR M15 une seule fois
        atr_cfg = cfg.get('atr_settings', {}).get('default', {})
        atr_period = atr_cfg.get('period', 14)
        atr_m15 = self.calculate_atr(ohlc, atr_period)
        
        if cfg.get('partial_tp', {}).get('enabled', False): self._apply_partial_tp(positions, tick, context, cfg.get('partial_tp', {}))
        if cfg.get('breakeven', {}).get('enabled', False): self._apply_breakeven(positions, tick, context, cfg.get('breakeven', {}), atr_m15)
        if cfg.get('trailing_stop_atr', {}).get('enabled', False): self._apply_trailing_stop_atr(positions, tick, atr_m15, cfg)

    def _apply_partial_tp(self, positions: list, tick, trade_context: dict, partial_tp_cfg: dict):
        # ... (Logique inchangée) ...
        levels = sorted(partial_tp_cfg.get('levels', []), key=lambda x: x.get('rr', 0))
        magic_number = self._config.get('trading_settings', {}).get('magic_number', 0)
        if not levels: return
        for pos in positions:
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
                rr_target, pct_target = lvl.get('rr', 0), lvl.get('percent', 0) / 100.0
                if current_rr >= rr_target and closed_pct < (pct_target - 0.001): # Tolérance float
                    target_lvl = lvl; break # Viser le niveau le plus élevé non atteint
            if not target_lvl: continue
            
            total_pct_close = target_lvl.get('percent', 0) / 100.0
            pct_now = total_pct_close - closed_pct
            if pct_now <= 0.001: continue
            
            vol_close = original_volume * pct_now
            rr_lbl = target_lvl.get('rr')
            self.log.info(f"TP PARTIEL (R{rr_lbl}) #{pos.ticket} (RR:{current_rr:.2f}). Clôture {pct_now*100:.1f}% ({vol_close:.2f} lots).")
            res = self._executor.close_partial_position(pos.ticket, vol_close, magic_number, f"Partial TP R{rr_lbl}")
            if res:
                self._executor.update_trade_context_partials(pos.ticket, pct_now)
                is_tp1 = (target_lvl.get('rr') == levels[0].get('rr'))
                move_sl, be_pips = partial_tp_cfg.get('move_sl_to_be_after_tp1', True), partial_tp_cfg.get('be_pips_plus_after_tp1', 5)
                
                if is_tp1 and move_sl:
                     be_sl = pos.price_open + (be_pips * self.point) if pos.type == BUY else pos.price_open - (be_pips * self.point)
                     is_better = (pos.type == BUY and be_sl > pos.sl) or (pos.type == SELL and (pos.sl == 0 or be_sl < pos.sl))
                     if is_better: self.log.info(f"SL->BE+ ({be_sl:.5f}) après TP1 #{pos.ticket}"); self._executor.modify_position(pos.ticket, be_sl, pos.tp)

    def _apply_breakeven(self, positions: list, tick, trade_context: dict, cfg: dict, atr_m15: Optional[float]):
        # ... (Logique inchangée) ...
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
        """(J.7) Modifié pour utiliser atr_m15 précalculé + Fix Logique SELL."""
        ts_cfg = cfg.get('trailing_stop_atr', {})
        if atr_m15 is None or atr_m15 <= 0: 
            self.log.debug("TS ATR ignoré: ATR M15 invalide.")
            return
            
        act_multi, trail_multi = ts_cfg.get('activation_multiple', 2.0), ts_cfg.get('trailing_multiple', 1.8)
        
        for pos in positions:
            new_sl, profit = pos.sl, 0.0
            if pos.type == BUY:
                profit = tick.bid - pos.price_open
                if profit >= (atr_m15 * act_multi):
                    potential_sl = tick.bid - (atr_m15 * trail_multi)
                    if potential_sl > pos.sl: new_sl = potential_sl
            elif pos.type == SELL:
                profit = pos.price_open - tick.ask
                if profit >= (atr_m15 * act_multi):
                    potential_sl = tick.ask + (atr_m15 * trail_multi)
                    # (Fix J.7) Comparer potential_sl au SL actuel (pos.sl), pas à new_sl (qui = pos.sl)
                    if (pos.sl == 0 or potential_sl < pos.sl): 
                        new_sl = potential_sl 
            
            if new_sl != pos.sl:
                self.log.info(f"TS ATR: SL #{pos.ticket} -> {new_sl:.{self.digits}f} (Profit Pips: {profit/self.point:.1f}, ATR: {atr_m15:.5f})")
                self._executor.modify_position(pos.ticket, new_sl, pos.tp)