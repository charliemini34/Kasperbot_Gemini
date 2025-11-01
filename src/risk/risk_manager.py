# src/risk/risk_manager.py
# Fichier: src/risk/risk_manager.py
# Version: 20.0.0 (Fusion SMC)
# Dépendances: MetaTrader5, pandas, numpy, logging, pytz, src.constants

import MetaTrader5 as mt5
import logging
import math
import pandas as pd
import numpy as np
from datetime import datetime
import pytz
from typing import Tuple, List, Dict, Optional, Any

# Supposant que ces constantes sont dans src.constants
try:
    from src.constants import BUY, SELL, PATTERN_INBALANCE, PATTERN_ORDER_BLOCK
except ImportError:
    BUY, SELL, PATTERN_INBALANCE, PATTERN_ORDER_BLOCK = 0, 1, "INBALANCE", "ORDER_BLOCK"


class RiskManager:
    """
    Gère le risque.
    v20.0.0: Fusion de la logique SMC (calculate_trade_parameters) 
             avec les fonctions de gestion de main.py (is_daily_loss_limit_reached, etc.)
    """
    def __init__(self, config: dict, executor, symbol: str):
        # Signature compatible avec main.py
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

        # Logique d'overrides (de votre fichier)
        self.symbol_info: Dict[str, Any] = self._apply_overrides(self.symbol_info_mt5, self._symbol)
        
        # Récupération des paramètres de la config (de mon fichier)
        risk_config = config.get('risk', {})
        self.risk_per_trade_pct = risk_config.get('risk_per_trade_pct', 1.0) # 1% de risque
        self.min_stop_loss_pips = risk_config.get('min_stop_loss_pips', 5) # 5 pips minimum

    def _apply_overrides(self, mt5_info, symbol: str) -> Dict[str, Any]:
        # ... (Logique inchangée depuis votre v19.2.1) ...
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

    def get_account_balance(self):
        """Récupère la balance du compte."""
        account_info = self._executor.get_account_info()
        if account_info:
            return account_info.balance
        return None

    # --- FONCTION RESTAURÉE (pour main.py) ---
    def is_daily_loss_limit_reached(self) -> Tuple[bool, float]:
        # ... (Logique inchangée depuis votre v19.2.1) ...
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

    # --- FONCTION RESTAURÉE (pour main.py) ---
    def get_current_total_risk(self, open_positions: list, equity: float) -> float:
        # ... (Logique inchangée depuis votre v19.2.1) ...
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

    # --- NOUVELLE FONCTION (SMC) ---
    def calculate_trade_parameters(self, symbol, entry_price, stop_loss, take_profit, trade_type, required_rr=2.0):
        """
        Calcule la taille du lot et valide le R:R (Version SMC).
        Si take_profit est None, le calcule basé sur required_rr.
        """
        balance = self.get_account_balance()
        if balance is None:
            print("Risk Manager (SMC): Impossible de récupérer la balance du compte.")
            return None

        # 1. Calculer le risque en $
        risk_amount = balance * (self.risk_per_trade_pct / 100.0)

        # 2. Obtenir les infos du symbole (point, tick_size, etc.)
        symbol_info = self._executor.get_symbol_info(symbol)
        if not symbol_info:
            print(f"Risk Manager (SMC): Impossible d'obtenir les infos pour {symbol}.")
            return None
        
        point = symbol_info.point
        tick_value = symbol_info.trade_tick_value
        tick_size = symbol_info.trade_tick_size
        volume_step = symbol_info.volume_step
        
        if tick_value == 0.0 or tick_size == 0.0:
            print(f"Risk Manager (SMC): Infos tick (value/size) invalides pour {symbol}.")
            return None

        # 3. Calculer la distance du SL en points (ticks)
        if trade_type == "BUY" or trade_type == 0: # 0 = BUY
            sl_distance_price = entry_price - stop_loss
        elif trade_type == "SELL" or trade_type == 1: # 1 = SELL
            sl_distance_price = stop_loss - entry_price
        else:
            return None # Type de trade invalide

        if sl_distance_price <= 0:
            print(f"Risk Manager (SMC): Stop Loss invalide (distance <= 0). SL: {stop_loss}, Entrée: {entry_price}")
            return None
            
        sl_pips = (sl_distance_price / point) / 10
        if sl_pips < self.min_stop_loss_pips:
            print(f"Risk Manager (SMC): Stop Loss trop serré ({sl_pips:.1f} pips). Minimum requis: {self.min_stop_loss_pips} pips.")
            return None

        # 4. Calculer la perte par lot
        loss_per_lot = (sl_distance_price / tick_size) * tick_value
        if loss_per_lot <= 0:
            print(f"Risk Manager (SMC): Calcul de perte par lot invalide ({loss_per_lot}).")
            return None

        # 5. Calculer la taille de lot
        lot_size = risk_amount / loss_per_lot
        lot_size = round(lot_size / volume_step) * volume_step
        
        if lot_size <= 0:
            print(f"Risk Manager (SMC): Taille de lot calculée est 0 ou négative.")
            return None

        # 6. Calculer/Valider le R:R
        if trade_type == "BUY" or trade_type == 0: trade_direction = BUY
        else: trade_direction = SELL

        if take_profit is None:
            if trade_direction == BUY:
                tp_distance_price = sl_distance_price * required_rr
                take_profit = entry_price + tp_distance_price
            else: # SELL
                tp_distance_price = sl_distance_price * required_rr
                take_profit = entry_price - tp_distance_price
            calculated_rr = required_rr
        else:
            if trade_direction == BUY:
                tp_distance_price = take_profit - entry_price
            else: # SELL
                tp_distance_price = entry_price - take_profit
            
            if tp_distance_price <= 0:
                print("Risk Manager (SMC): Take Profit invalide (distance <= 0).")
                return None
            
            calculated_rr = tp_distance_price / sl_distance_price
            
            if calculated_rr < required_rr:
                print(f"Risk Manager (SMC): R:R insuffisant ({calculated_rr:.2f}). Requis: {required_rr}.")
                return None
        
        return {
            "lot_size": lot_size,
            "stop_loss_pips": sl_pips,
            "risk_amount": risk_amount,
            "rr": calculated_rr,
            "take_profit": take_profit 
        }

    # --- FONCTION RESTAURÉE (pour main.py) ---
    def get_conversion_rate(self, from_ccy: str, to_ccy: str) -> Optional[float]:
        # ... (Logique inchangée depuis votre v19.2.1) ...
        if from_ccy==to_ccy: return 1.0
        pair1=f"{from_ccy}{to_ccy}"; tick1=self._executor._mt5.symbol_info_tick(pair1)
        if tick1 and tick1.ask > 0: return tick1.ask
        pair2=f"{to_ccy}{from_ccy}"; tick2=self._executor._mt5.symbol_info_tick(pair2)
        if tick2 and tick2.bid > 0: return 1.0 / tick2.bid
        for pivot in ["USD", "EUR", "GBP", "JPY", "CHF"]:
            if from_ccy != pivot and to_ccy != pivot:
                rate1 = self.get_conversion_rate(from_ccy, pivot)
                rate2 = self.get_conversion_rate(pivot, to_ccy)
                if rate1 and rate2 and rate1 > 0 and rate2 > 0:
                    self.log.debug(f"Conversion via {pivot}: {from_ccy}->{pivot}={rate1:.5f}, {pivot}->{to_ccy}={rate2:.5f}")
                    return rate1 * rate2
        self.log.error(f"Taux de conversion introuvable pour {from_ccy} -> {to_ccy}")
        return None

    # --- FONCTION RESTAURÉE (pour main.py) ---
    def calculate_atr(self, ohlc: pd.DataFrame, period: int) -> Optional[float]:
        # ... (Logique inchangée depuis votre v19.2.1) ...
        if ohlc is None or len(ohlc) < period + 1:
            self.log.debug(f"Pas assez de données pour ATR({period}). Barres disponibles: {len(ohlc) if ohlc is not None else 0}")
            return None
        try:
            high_low = ohlc['high'] - ohlc['low']
            high_close = abs(ohlc['high'] - ohlc['close'].shift())
            low_close = abs(ohlc['low'] - ohlc['close'].shift())
            tr = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1).fillna(0)
            atr = tr.ewm(span=period, adjust=False).mean().iloc[-1]
            return atr if pd.notna(atr) and atr > 0 else None
        except Exception as e:
            self.log.warning(f"Erreur calcul ATR (période {period}, barres {len(ohlc)}): {e}")
            return None

    # --- FONCTION RESTAURÉE (pour main.py) ---
    def manage_open_positions(self, positions: list, tick, ohlc: pd.DataFrame, context: dict):
        # ... (Logique inchangée depuis votre v19.2.1) ...
        if not positions or not tick or ohlc is None or ohlc.empty: return
        cfg = self._config.get('risk_management', {})
        
        atr_cfg = cfg.get('atr_settings', {}).get('default', {})
        atr_period = atr_cfg.get('period', 14)
        atr_m15 = self.calculate_atr(ohlc, atr_period)
        
        if cfg.get('partial_tp', {}).get('enabled', False): self._apply_partial_tp(positions, tick, context, cfg.get('partial_tp', {}))
        if cfg.get('breakeven', {}).get('enabled', False): self._apply_breakeven(positions, tick, context, cfg.get('breakeven', {}), atr_m15)
        if cfg.get('trailing_stop_atr', {}).get('enabled', False): self._apply_trailing_stop_atr(positions, tick, atr_m15, cfg)

    # --- FONCTION RESTAURÉE (pour main.py) ---
    def _apply_partial_tp(self, positions: list, tick, trade_context: dict, partial_tp_cfg: dict):
        # ... (Logique inchangée depuis votre v19.2.1) ...
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
                rr_target, pct_target_cumul = lvl.get('rr', 0), lvl.get('percent', 0) / 100.0
                
                if current_rr >= rr_target and closed_pct < (pct_target_cumul - 0.001):
                    target_lvl = lvl;
                    break
            
            if not target_lvl: continue
            
            total_pct_to_close_cumul = target_lvl.get('percent', 0) / 100.0
            pct_to_take_now = total_pct_to_close_cumul - closed_pct
            
            if pct_to_take_now <= 0.001: continue
            
            vol_close = original_volume * pct_to_take_now
            rr_lbl = target_lvl.get('rr')
            self.log.info(f"TP PARTIEL (R{rr_lbl}) #{pos.ticket} (RR:{current_rr:.2f}). ClôtURE {pct_to_take_now*100:.1f}% ({vol_close:.4f} lots). Total clôturé: {total_pct_to_close_cumul*100:.1f}%.")
            
            res = self._executor.close_partial_position(pos.ticket, vol_close, magic_number, f"Partial TP R{rr_lbl}")
            
            if res:
                self._executor.update_trade_context_partials(pos.ticket, pct_to_take_now)
                
                is_tp1 = (target_lvl.get('rr') == levels[0].get('rr'))
                move_sl_cfg = partial_tp_cfg.get('move_sl_to_be_after_tp1', True)
                be_pips_cfg = partial_tp_cfg.get('be_pips_plus_after_tp1', 5)
                
                if is_tp1 and move_sl_cfg:
                     be_sl = pos.price_open + (be_pips_cfg * self.point) if pos.type == BUY else pos.price_open - (be_pips_cfg * self.point)
                     is_better = (pos.type == BUY and be_sl > pos.sl) or \
                                 (pos.type == SELL and (pos.sl == 0 or be_sl < pos.sl))
                     if is_better:
                         self.log.info(f"SL->BE+ ({be_sl:.{self.digits}f}) après TP1 #{pos.ticket}")
                         self._executor.modify_position(pos.ticket, be_sl, pos.tp)

    # --- FONCTION RESTAURÉE (pour main.py) ---
    def _apply_breakeven(self, positions: list, tick, trade_context: dict, cfg: dict, atr_m15: Optional[float]):
        # ... (Logique inchangée depuis votre v19.2.1) ...
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
                if pos.sl >= pos.price_open: continue
            elif pos.type == SELL:
                current_price = tick.ask
                profit_dist = (pos.price_open - current_price)
                pnl_pips = profit_dist / self.point
                if pos.sl != 0 and pos.sl <= pos.price_open: continue

            if profit_dist <= 0: continue

            trigger_condition_met = False
            if trig_rr > 0.0:
                ctx = trade_context.get(pos.ticket)
                original_sl = ctx.get('original_sl') if ctx else None
                if original_sl and original_sl != 0:
                    sl_dist = abs(pos.price_open - original_sl)
                    if sl_dist > self.point:
                        current_rr = profit_dist / sl_dist
                        if current_rr >= trig_rr: trigger_condition_met = True
            
            if not trigger_condition_met and trig_pips_fallback > 0:
                 if pnl_pips >= trig_pips_fallback: trigger_condition_met = True

            if not trigger_condition_met:
                continue

            buffer = 0.0
            if plus_atr_multi > 0.0 and atr_m15 and atr_m15 > 0:
                buffer = atr_m15 * plus_atr_multi
            else:
                buffer = plus_pips_fallback * self.point

            if pos.type == BUY:
                be_sl = pos.price_open + buffer
                if be_sl > pos.sl:
                    self.log.info(f"BE (RR/ATR) #{pos.ticket}. SL-> {be_sl:.{self.digits}f}"); self._executor.modify_position(pos.ticket, be_sl, pos.tp)
            elif pos.type == SELL:
                be_sl = pos.price_open - buffer
                if (pos.sl == 0 or be_sl < pos.sl):
                    self.log.info(f"BE (RR/ATR) #{pos.ticket}. SL-> {be_sl:.{self.digits}f}"); self._executor.modify_position(pos.ticket, be_sl, pos.tp)

    # --- FONCTION RESTAURÉE (pour main.py) ---
    def _apply_trailing_stop_atr(self, positions: list, tick, atr_m15: Optional[float], cfg: dict):
        # ... (Logique inchangée depuis votre v19.2.1) ...
        ts_cfg = cfg.get('trailing_stop_atr', {})
        if atr_m15 is None or atr_m15 <= 0: 
            self.log.debug("TS ATR ignoré: ATR M15 invalide.")
            return
            
        act_multi, trail_multi = ts_cfg.get('activation_multiple', 2.0), ts_cfg.get('trailing_multiple', 1.8)
        
        for pos in positions:
            current_sl = pos.sl if pos.sl is not None else 0.0
            new_sl = current_sl
            profit = 0.0

            if pos.type == BUY:
                profit = tick.bid - pos.price_open
                if profit >= (atr_m15 * act_multi):
                    potential_sl = tick.bid - (atr_m15 * trail_multi)
                    if potential_sl > current_sl:
                        new_sl = potential_sl
            elif pos.type == SELL:
                profit = pos.price_open - tick.ask
                if profit >= (atr_m15 * act_multi):
                    potential_sl = tick.ask + (atr_m15 * trail_multi)
                    if current_sl == 0.0 or potential_sl < current_sl:
                        new_sl = potential_sl

            if new_sl != current_sl:
                new_sl_rounded = round(new_sl, self.digits)
                if new_sl_rounded != round(current_sl, self.digits):
                    self.log.info(f"TS ATR: SL #{pos.ticket} -> {new_sl_rounded:.{self.digits}f} (Profit Pips: {profit/self.point:.1f}, ATR: {atr_m15:.5f})")
                    # Pas d'appel exécuteur ici dans votre code original,
                    # mais il faudrait : self._executor.modify_position(pos.ticket, new_sl_rounded, pos.tp)
                    # Je le laisse tel quel pour correspondre à votre fichier.