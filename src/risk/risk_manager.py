# Fichier: src/risk/risk_manager.py
# Version: 19.0.8 (Fix R15 - Override Key Mapping)
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
    Gère le risque avec entrée limite SMC (R7) et override contrat (R4).
    v19.0.8: Corrige mapping clé override contract_size (R15).
    """
    def __init__(self, config: dict, executor, symbol: str):
        self.log = logging.getLogger(self.__class__.__name__)
        self._config: Dict = config
        self._executor = executor
        self._symbol: str = symbol

        self.symbol_info_mt5 = self._executor._mt5.symbol_info(self._symbol)
        self.account_info = self._executor._mt5.account_info()

        if not self.symbol_info_mt5 or not self.account_info:
            self.log.critical(f"Infos MT5 indispo pour {self._symbol} ou compte.")
            raise ValueError("Infos MT5 manquantes.")

        self.point: float = self.symbol_info_mt5.point
        self.digits: int = self.symbol_info_mt5.digits

        self.symbol_info: Dict[str, Any] = self._apply_overrides(self.symbol_info_mt5)

    # --- R15 : Fonction modifiée pour gérer le mapping des clés ---
    def _apply_overrides(self, mt5_info) -> Dict[str, Any]:
        """Applique overrides, gère mapping clés, retourne DICTIONNAIRE."""
        overrides = self._config.get('symbol_overrides', {}).get(self._symbol, {})
        info_dict = {}
        fields_to_copy = [ # Clés MT5
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
            # --- FIX R15: Mapper la clé du config vers la clé MT5 ---
            mt5_key = config_key
            if config_key == 'contract_size':
                mt5_key = 'trade_contract_size'
            # Ajoutez d'autres mappings si nécessaire
            # --- FIN FIX R15 ---

            if mt5_key in info_dict: # Vérifier avec la clé MT5 mappée
                try:
                    original_type = type(info_dict[mt5_key])
                    info_dict[mt5_key] = original_type(value) # Appliquer sur la clé MT5
                    applied_list.append(f"{mt5_key}({config_key})={info_dict[mt5_key]}") # Log informatif
                except Exception as e:
                     self.log.error(f"Erreur override '{config_key}={value}' (->{mt5_key}) pour {self._symbol}: {e}")
            else:
                 self.log.warning(f"Override ignoré: Attribut '{config_key}' (mappé vers '{mt5_key}') non trouvé/copié pour {self._symbol}.")

        if applied_list:
             self.log.info(f"Overrides pour {self._symbol}: {', '.join(applied_list)}")
        return info_dict
    # --- Fin R15 ---

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

    def calculate_trade_parameters(self, equity: float, current_tick, ohlc_data: pd.DataFrame, trade_signal: dict) -> Tuple[float, float, float, float]:
        # ... (Logique inchangée) ...
        try:
            req_keys=['direction','entry_zone_start','entry_zone_end','stop_loss_level','target_price']
            if not isinstance(trade_signal,dict) or not all(k in trade_signal for k in req_keys): self.log.error(f"Signal SMC invalide: {trade_signal}"); return 0.0,0.0,0.0,0.0
            direction,pattern=trade_signal['direction'],trade_signal['pattern']
            z_start,z_end=trade_signal['entry_zone_start'],trade_signal['entry_zone_end']
            sl_struct,tp_target=trade_signal['stop_loss_level'],trade_signal['target_price']
            entry_limit=self._calculate_limit_entry_price(z_start,z_end,pattern,direction)
            sl_final=self._calculate_final_sl(sl_struct,direction)
            tp_final=round(tp_target,self.digits)
            if entry_limit==0.0 or sl_final==0.0: return 0.0,0.0,0.0,0.0
            current_price=current_tick.ask if direction==BUY else current_tick.bid
            if (direction==BUY and current_price<entry_limit) or (direction==SELL and current_price>entry_limit): self.log.warning(f"Retracement manqué {self._symbol}. Actuel={current_price:.5f}, Limite={entry_limit:.5f}."); return 0.0,0.0,0.0,0.0
            min_sl_dist=self.point*5
            if abs(entry_limit-sl_final)<min_sl_dist: self.log.error(f"SL trop proche. SL={sl_final:.5f}, Limite={entry_limit:.5f}. Annulé."); return 0.0,0.0,0.0,0.0
            if (direction==BUY and tp_final<=entry_limit) or (direction==SELL and tp_final>=entry_limit): self.log.error(f"TP invalide vs Limite. TP={tp_final:.5f}, Limite={entry_limit:.5f}. Annulé."); return 0.0,0.0,0.0,0.0
            risk_pct=self._config.get('risk_management',{}).get('risk_per_trade',0.01)
            volume=self._calculate_volume(equity,risk_pct,entry_limit,sl_final)
            if volume<self.symbol_info_mt5.volume_min: self.log.warning(f"Volume ({volume:.4f}) < Min MT5 ({self.symbol_info_mt5.volume_min}). Annulé."); return 0.0,0.0,0.0,0.0
            self.log.info(f"Params Ordre Limite OK: {direction} {volume:.2f} @ {entry_limit:.5f}, SL={sl_final:.5f}, TP={tp_final:.5f}")
            return volume,entry_limit,sl_final,tp_final
        except Exception as e: self.log.error(f"Erreur calcul params limite : {e}",exc_info=True); return 0.0,0.0,0.0,0.0

    def _calculate_limit_entry_price(self, z_start, z_end, pattern, direction) -> float:
        # ... (Logique inchangée) ...
        cfg=self.config.get('pattern_detection',{}).get('entry_logic',{});level=0.5
        try:
            if pattern==PATTERN_INBALANCE: level=cfg.get('fvg_entry_level',0.5)
            elif pattern==PATTERN_ORDER_BLOCK: level=cfg.get('ob_entry_level',0.0)
            zone_min,zone_max=min(z_start,z_end),max(z_start,z_end);zone_size=zone_max-zone_min
            entry=zone_max-(zone_size*level) if direction==BUY else zone_min+(zone_size*level)
            return round(entry,self.digits)
        except Exception as e: self.log.error(f"Erreur calcul entrée limite {pattern}: {e}"); return 0.0

    def _calculate_final_sl(self, sl_structural, direction) -> float:
        # ... (Logique inchangée) ...
        buffer_pips=self._config.get('risk_management',{}).get('sl_buffer_pips',5);buffer=buffer_pips*self.point
        sl=sl_structural-buffer if direction==BUY else sl_structural+buffer
        return round(sl,self.digits)

    def _calculate_volume(self, equity: float, risk_pct: float, entry: float, sl: float) -> float:
        # ... (Logique inchangée depuis v19.0.5) ...
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
        if final_vol<vol_min: self.log.warning(f"Vol final {final_vol:.4f} < Min MT5 {vol_min}. Annulé."); return 0.0
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
        hl=ohlc['high']-ohlc['low']; hc=abs(ohlc['high']-ohlc['close'].shift()); lc=abs(ohlc['low']-ohlc['close'].shift())
        tr=pd.concat([hl,hc,lc],axis=1).max(axis=1)
        return tr.ewm(span=period,adjust=False).mean().iloc[-1]

    def manage_open_positions(self, positions: list, tick, ohlc: pd.DataFrame, context: dict):
        # ... (Logique inchangée) ...
        if not positions or not tick or ohlc is None or ohlc.empty: return
        cfg = self._config.get('risk_management', {})
        if cfg.get('partial_tp', {}).get('enabled', False): self._apply_partial_tp(positions, tick, context, cfg.get('partial_tp', {}))
        if cfg.get('breakeven', {}).get('enabled', False): self._apply_breakeven(positions, tick, cfg.get('breakeven', {}))
        if cfg.get('trailing_stop_atr', {}).get('enabled', False): self._apply_trailing_stop_atr(positions, tick, ohlc, cfg)

    def _apply_partial_tp(self, positions: list, tick, trade_context: dict, partial_tp_cfg: dict):
        # ... (Logique inchangée depuis v19.0.6 - Fix R13 OK) ...
        levels = sorted(partial_tp_cfg.get('levels', []), key=lambda x: x.get('rr', 0))
        magic_number = self._config.get('trading_settings', {}).get('magic_number', 0)
        if not levels: return
        for pos in positions:
            ctx = trade_context.get(pos.ticket)
            if not ctx: continue
            original_sl, original_volume, closed_pct = ctx.get('original_sl'), ctx.get('original_volume'), ctx.get('partial_tp_taken_percent', 0.0)
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
                if current_rr >= rr_target and closed_pct < pct_target: target_lvl = lvl; break
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
                move_sl, be_pips = partial_tp_cfg.get('move_sl_to_be_after_tp1', True), partial_tp_cfg.get('be_pips_plus_after_tp1', 5)
                if rr_lbl == levels[0].get('rr') and move_sl:
                     be_sl = pos.price_open + (be_pips * self.point) if pos.type == BUY else pos.price_open - (be_pips * self.point)
                     is_better = (pos.type == BUY and be_sl > pos.sl) or (pos.type == SELL and (pos.sl == 0 or be_sl < pos.sl))
                     if is_better: self.log.info(f"SL->BE+ ({be_sl:.5f}) après TP1 #{pos.ticket}"); self._executor.modify_position(pos.ticket, be_sl, pos.tp)

    def _apply_breakeven(self, positions: list, tick, cfg: dict):
        # ... (Logique inchangée depuis v19.0.6) ...
        trig_pips, plus_pips = cfg.get('trigger_pips', 100), cfg.get('pips_plus', 10)
        for pos in positions:
            pnl_pips, be_sl = 0.0, 0.0
            if pos.type == BUY:
                pnl_pips = (tick.bid - pos.price_open) / self.point
                if pos.sl < pos.price_open and pnl_pips >= trig_pips:
                    be_sl = pos.price_open + (plus_pips * self.point)
                    if be_sl > pos.sl: self.log.info(f"BE #{pos.ticket}. SL-> {be_sl:.{self.digits}f}"); self._executor.modify_position(pos.ticket, be_sl, pos.tp)
            elif pos.type == SELL:
                pnl_pips = (pos.price_open - tick.ask) / self.point
                if (pos.sl==0 or pos.sl > pos.price_open) and pnl_pips >= trig_pips:
                    be_sl = pos.price_open - (plus_pips * self.point)
                    if (pos.sl==0 or be_sl < pos.sl): self.log.info(f"BE #{pos.ticket}. SL-> {be_sl:.{self.digits}f}"); self._executor.modify_position(pos.ticket, be_sl, pos.tp)

    def _apply_trailing_stop_atr(self, positions: list, tick, ohlc: pd.DataFrame, cfg: dict):
        # ... (Logique inchangée depuis v19.0.6) ...
        ts_cfg, atr_cfg = cfg.get('trailing_stop_atr', {}), cfg.get('atr_settings', {}).get('default', {})
        period, atr = atr_cfg.get('period', 14), self.calculate_atr(ohlc, atr_cfg.get('period', 14))
        if atr is None or atr <= 0: return
        act_multi, trail_multi = ts_cfg.get('activation_multiple', 2.0), ts_cfg.get('trailing_multiple', 1.8)
        for pos in positions:
            new_sl, profit = pos.sl, 0.0
            if pos.type == BUY:
                profit = tick.bid - pos.price_open
                if profit >= (atr * act_multi):
                    potential_sl = tick.bid - (atr * trail_multi)
                    if potential_sl > pos.sl: new_sl = potential_sl
            elif pos.type == SELL:
                profit = pos.price_open - tick.ask
                if profit >= (atr * act_multi):
                    potential_sl = tick.ask + (atr * trail_multi)
                    if new_sl == 0 or potential_sl < new_sl: new_sl = potential_sl
            if new_sl != pos.sl:
                self.log.info(f"TS ATR: SL #{pos.ticket} -> {new_sl:.{self.digits}f} (Profit Pips: {profit/self.point:.1f}, ATR: {atr:.5f})")
                self._executor.modify_position(pos.ticket, new_sl, pos.tp)