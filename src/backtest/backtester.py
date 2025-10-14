# Fichier: src/backtest/backtester.py

import MetaTrader5 as mt5
import pandas as pd
from datetime import datetime
import logging
import yaml
import numpy as np

from src.patterns.pattern_detector import PatternDetector
from src.risk.risk_manager import RiskManager
from src.execution.mt5_executor import MT5Executor

class Backtester:
    """Module de backtesting v7.6 : Ne ferme plus la connexion MT5 principale."""
    def __init__(self, shared_state):
        self.state = shared_state
        self.log = logging.getLogger(self.__class__.__name__)
        # On vérifie que MT5 est bien initialisé, mais on ne le gère pas ici.
        if not mt5.terminal_info():
            self.log.error("Le Backtester ne peut pas démarrer car MT5 n'est pas connecté.")
            raise ConnectionError("MT5 n'est pas initialisé.")
            
    def run(self, start_date_str, end_date_str, initial_capital):
        self.log.info(f"Démarrage du backtest de {start_date_str} à {end_date_str}...")
        self.state.start_backtest()

        try:
            config = load_yaml('config.yaml')
            start_date = datetime.strptime(start_date_str, '%Y-%m-%d')
            end_date = datetime.strptime(end_date_str, '%Y-%m-%d')
            
            symbol = config['trading_settings']['symbol']
            timeframe_str = config['trading_settings']['timeframe']
            timeframe = getattr(mt5, f"TIMEFRAME_{timeframe_str.upper()}")
            
            all_data = mt5.copy_rates_range(symbol, timeframe, start_date, end_date)
            if all_data is None or len(all_data) < 200:
                raise ValueError("Pas assez de données historiques pour cette période.")
            
            df = pd.DataFrame(all_data)
            df['time'] = pd.to_datetime(df['time'], unit='s')
            
            equity = float(initial_capital)
            equity_curve, trades, open_position = [equity], [], None
            
            fake_executor = MT5Executor(mt5)
            risk_manager = RiskManager(config['risk_management'], fake_executor, symbol)
            detector = PatternDetector(config)

            total_bars = len(df)
            for i in range(200, total_bars):
                progress = ((i - 200) / (total_bars - 200)) * 100
                if i % (total_bars // 100 or 1) == 0:
                    self.state.update_backtest_progress(progress)

                current_data = df.iloc[:i+1]
                current_candle = df.iloc[i]
                
                if open_position:
                    closed, pnl = False, 0
                    if open_position['direction'] == 'BUY':
                        if current_candle['low'] <= open_position['sl']: closed, pnl = True, (open_position['sl'] - open_position['entry_price'])
                        elif current_candle['high'] >= open_position['tp']: closed, pnl = True, (open_position['tp'] - open_position['entry_price'])
                    else:
                        if current_candle['high'] >= open_position['sl']: closed, pnl = True, (open_position['entry_price'] - open_position['sl'])
                        elif current_candle['low'] <= open_position['tp']: closed, pnl = True, (open_position['entry_price'] - open_position['tp'])
                    
                    if closed:
                        equity += pnl * open_position['volume'] * risk_manager.symbol_info.trade_contract_size
                        open_position['pnl'] = pnl * open_position['volume'] * risk_manager.symbol_info.trade_contract_size
                        trades.append(open_position)
                        open_position = None
                        equity_curve.append(equity)
                
                if not open_position:
                    trade_signal = detector.detect_patterns(current_data)
                    if trade_signal:
                        entry_price = current_candle['close']
                        sl, tp = risk_manager.calculate_sl_tp(entry_price, trade_signal['direction'], current_data)
                        volume = risk_manager.calculate_volume(equity, entry_price, sl)
                        if volume > 0:
                            open_position = {'direction': trade_signal['direction'], 'entry_price': entry_price, 'sl': sl, 'tp': tp, 'volume': volume}

            final_pnl = equity - float(initial_capital)
            wins = [t for t in trades if t.get('pnl', 0) > 0]
            win_rate = (len(wins) / len(trades)) * 100 if trades else 0
            
            equity_series = pd.Series(equity_curve)
            peak = equity_series.expanding(min_periods=1).max()
            drawdown = ((equity_series - peak) / peak).min() if not peak.empty else 0

            results = {"pnl": final_pnl, "total_trades": len(trades), "win_rate": win_rate, "max_drawdown_percent": abs(drawdown * 100), "equity_curve": equity_curve}
            self.state.finish_backtest(results)
            self.log.info(f"Backtest terminé. PnL: {final_pnl:.2f}$")

        except Exception as e:
            self.log.error(f"Erreur durant le backtest: {e}", exc_info=True)
            self.state.finish_backtest({"error": str(e)})

def load_yaml(filepath: str) -> dict:
    with open(filepath, 'r', encoding='utf-8') as f:
        return yaml.safe_load(f)