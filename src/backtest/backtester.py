# Fichier: src/backtest/backtester.py
# Version: 4.0.0 (High-Fidelity & Flexible)
# Dépendances: pandas, numpy, yaml, MetaTrader5
# Description: Moteur de backtesting paramétrable et haute-fidélité.

import MetaTrader5 as mt5
import pandas as pd
import numpy as np
from datetime import datetime
import logging
import yaml

from src.patterns.pattern_detector import PatternDetector
from src.risk.risk_manager import RiskManager
from src.shared_state import SharedState

class MockConnector:
    def __init__(self, all_historical_data: dict):
        self._historical_data = all_historical_data

    def get_ohlc(self, symbol: str, timeframe: str, num_bars: int, end_time=None):
        if timeframe not in self._historical_data:
            return None
        df = self._historical_data[timeframe]
        if end_time:
            data_slice = df[df.index <= end_time].tail(num_bars)
        else:
            data_slice = df.tail(num_bars)
        if len(data_slice) < num_bars:
            return None
        return data_slice.reset_index()

class Backtester:
    def __init__(self, shared_state: SharedState):
        self.state = shared_state
        self.log = logging.getLogger(self.__class__.__name__)
        if not mt5.terminal_info():
            raise ConnectionError("MT5 n'est pas initialisé, le backtester ne peut pas démarrer.")

    def run(self, symbol: str, timeframe: str, start_date_str: str, end_date_str: str, initial_capital: float, config: dict):
        self.log.info(f"Démarrage du backtest pour {symbol} ({timeframe}) de {start_date_str} à {end_date_str}...")
        self.state.start_backtest()

        try:
            start_date = datetime.strptime(start_date_str, '%Y-%m-%d')
            end_date = datetime.strptime(end_date_str, '%Y-%m-%d')
            main_timeframe_mt5 = getattr(mt5, f"TIMEFRAME_{timeframe.upper()}")
            
            all_data = {}
            main_df_rates = mt5.copy_rates_range(symbol, main_timeframe_mt5, start_date, end_date)
            if main_df_rates is None or len(main_df_rates) < 200:
                raise ValueError(f"Pas assez de données historiques pour {symbol} sur le timeframe {timeframe}.")
            
            main_df = pd.DataFrame(main_df_rates)
            main_df['time'] = pd.to_datetime(main_df['time'], unit='s')
            main_df.set_index('time', inplace=True)
            all_data[timeframe] = main_df

            if config.get('trend_filter', {}).get('enabled', False):
                htf_str = config['trend_filter']['higher_timeframe']
                htf_mt5 = getattr(mt5, f"TIMEFRAME_{htf_str.upper()}")
                htf_rates = mt5.copy_rates_range(symbol, htf_mt5, start_date, end_date)
                if htf_rates is None or len(htf_rates) == 0:
                    raise ValueError(f"Pas assez de données historiques pour le timeframe supérieur {htf_str}.")
                htf_df = pd.DataFrame(htf_rates)
                htf_df['time'] = pd.to_datetime(htf_df['time'], unit='s')
                htf_df.set_index('time', inplace=True)
                all_data[htf_str] = htf_df

            mock_connector = MockConnector(all_data)
            detector = PatternDetector(config)
            
            class MockExecutor:
                def __init__(self, symbol):
                    self._mt5 = mt5
                    self.account_info = mt5.account_info()
                    self.symbol_info = mt5.symbol_info(symbol)
                def modify_position(self, ticket, sl, tp): pass 
            
            risk_manager = RiskManager(config, MockExecutor(symbol), symbol)
            equity = float(initial_capital)
            equity_curve, trades, open_position = [equity], [], None
            total_bars = len(main_df)

            for i in range(200, total_bars):
                progress = ((i - 200) / (total_bars - 200)) * 100
                if i % 20 == 0: self.state.update_backtest_progress(progress)

                current_time = main_df.index[i]
                current_candle = main_df.iloc[i]
                current_data_slice = main_df.iloc[:i+1]
                
                if open_position:
                    closed, pnl = False, 0
                    if open_position['direction'] == "BUY":
                        if current_candle['low'] <= open_position['sl']: closed, pnl = True, open_position['sl'] - open_position['entry_price']
                        elif current_candle['high'] >= open_position['tp']: closed, pnl = True, open_position['tp'] - open_position['entry_price']
                    else: # SELL
                        if current_candle['high'] >= open_position['sl']: closed, pnl = True, open_position['entry_price'] - open_position['sl']
                        elif current_candle['low'] <= open_position['tp']: closed, pnl = True, open_position['entry_price'] - open_position['tp']

                    if closed:
                        trade_pnl = pnl * open_position['volume'] * risk_manager.symbol_info.trade_contract_size
                        equity += trade_pnl
                        open_position['pnl'] = trade_pnl
                        open_position['close_time'] = current_time
                        trades.append(open_position)
                        open_position = None
                        equity_curve.append(equity)
                
                if not open_position:
                    trade_signal = detector.detect_patterns(current_data_slice, mock_connector, symbol)
                    
                    if trade_signal:
                        entry_price = current_candle['close']
                        sl, tp = risk_manager.calculate_sl_tp(entry_price, trade_signal['direction'], current_data_slice, symbol)
                        volume = risk_manager.calculate_volume(equity, entry_price, sl)
                        
                        if volume > 0:
                            open_position = {
                                'direction': trade_signal['direction'], 'pattern': trade_signal['pattern'],
                                'entry_price': entry_price, 'sl': sl, 'tp': tp, 'volume': volume,
                                'open_time': current_time
                            }

            final_pnl = equity - float(initial_capital)
            wins = [t for t in trades if t.get('pnl', 0) > 0]
            win_rate = (len(wins) / len(trades)) * 100 if trades else 0
            equity_series = pd.Series(equity_curve)
            peak = equity_series.expanding(min_periods=1).max()
            drawdown = ((equity_series - peak) / peak).min() if not peak.empty else 0
            results = {"pnl": final_pnl, "total_trades": len(trades), "win_rate": win_rate, "max_drawdown_percent": abs(drawdown * 100), "equity_curve": equity_curve}
            self.state.finish_backtest(results)
            self.log.info(f"Backtest terminé. PnL final: {final_pnl:.2f}$")

        except Exception as e:
            self.log.error(f"Erreur durant le backtest: {e}", exc_info=True)
            self.state.finish_backtest({"error": str(e)})