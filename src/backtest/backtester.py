# Fichier: src/backtest/backtester.py

import MetaTrader5 as mt5
import pandas as pd
from datetime import datetime
import logging
import yaml
import numpy as np

from src.scorer.strategy_scorer import StrategyScorer
from src.scorer.aggregator import Aggregator
from src.risk.risk_manager import RiskManager
from src.execution.mt5_executor import MT5Executor # Nécessaire pour initialiser le RiskManager

class Backtester:
    def __init__(self, shared_state):
        self.state = shared_state
        self.log = logging.getLogger(self.__class__.__name__)
        # On a besoin d'une connexion MT5 active pour les infos de symboles
        if not mt5.initialize():
            self.log.error("Impossible d'initialiser MT5 pour le backtester.")
            raise ConnectionError("MT5 n'est pas lancé ou la connexion a échoué.")
            
    def run(self, start_date_str, end_date_str, initial_capital):
        self.log.info(f"Démarrage du backtest de {start_date_str} à {end_date_str}...")
        self.state.start_backtest()

        try:
            config = load_yaml('config.yaml')
            profiles = load_yaml('profiles.yaml')
            
            active_profile_name = config['trading_logic']['active_profile']
            strategy_weights = profiles.get(active_profile_name, profiles['custom'])

            start_date = datetime.strptime(start_date_str, '%Y-%m-%d')
            end_date = datetime.strptime(end_date_str, '%Y-%m-%d')
            
            self.log.info("Récupération des données historiques...")
            symbol = config['trading_settings']['symbol']
            timeframe = getattr(mt5, f"TIMEFRAME_{config['trading_settings']['timeframe'].upper()}")
            
            all_data = mt5.copy_rates_range(symbol, timeframe, start_date, end_date)
            if all_data is None or len(all_data) < 200:
                raise ValueError("Pas assez de données historiques pour cette période.")
            
            df = pd.DataFrame(all_data)
            df['time'] = pd.to_datetime(df['time'], unit='s')
            
            # --- Initialisation pour une simulation réaliste ---
            capital = float(initial_capital)
            equity = capital
            equity_curve = [equity]
            trades = []
            open_position = None
            
            # On simule un Executor et un RiskManager pour des calculs précis
            fake_executor = MT5Executor(mt5, None) 
            risk_manager = RiskManager(config['risk_management'], fake_executor, symbol)
            scorer = StrategyScorer()
            aggregator = Aggregator(strategy_weights)
            
            total_bars = len(df)
            for i in range(200, total_bars):
                progress = (i - 200) / (total_bars - 200) * 100
                if i % (total_bars // 100 or 1) == 0:
                    self.state.update_backtest_progress(progress)

                current_data = df.iloc[:i]
                current_price = df.iloc[i]
                
                # --- Gestion de la position ouverte ---
                if open_position:
                    closed = False
                    pnl = 0
                    if open_position['direction'] == 'BUY':
                        pnl = (current_price['close'] - open_position['entry_price']) * open_position['volume'] * risk_manager.symbol_info.trade_contract_size
                        if current_price['low'] <= open_position['sl']:
                            pnl = (open_position['sl'] - open_position['entry_price']) * open_position['volume'] * risk_manager.symbol_info.trade_contract_size
                            closed = True
                        elif current_price['high'] >= open_position['tp']:
                            pnl = (open_position['tp'] - open_position['entry_price']) * open_position['volume'] * risk_manager.symbol_info.trade_contract_size
                            closed = True
                    else: # SELL
                        pnl = (open_position['entry_price'] - current_price['close']) * open_position['volume'] * risk_manager.symbol_info.trade_contract_size
                        if current_price['high'] >= open_position['sl']:
                            pnl = (open_position['entry_price'] - open_position['sl']) * open_position['volume'] * risk_manager.symbol_info.trade_contract_size
                            closed = True
                        elif current_price['low'] <= open_position['tp']:
                            pnl = (open_position['entry_price'] - open_position['tp']) * open_position['volume'] * risk_manager.symbol_info.trade_contract_size
                            closed = True
                    
                    if closed:
                        equity += pnl
                        open_position['pnl'] = pnl
                        trades.append(open_position)
                        open_position = None
                        equity_curve.append(equity)
                
                # --- Recherche d'une nouvelle opportunité ---
                if not open_position:
                    raw_scores = scorer.calculate_all(current_data)
                    final_score, trade_direction = aggregator.calculate_final_score(raw_scores)
                    
                    if final_score >= config['trading_logic']['execution_threshold'] and trade_direction != "NEUTRAL":
                        entry_price = current_price['close']
                        
                        sl, tp = risk_manager.calculate_sl_tp(entry_price, trade_direction, current_data)
                        volume = risk_manager.calculate_volume(equity, entry_price, sl)
                        
                        if volume > 0:
                            open_position = {
                                'direction': trade_direction, 'entry_price': entry_price, 
                                'sl': sl, 'tp': tp, 'volume': volume,
                            }

            # --- Calcul des résultats finaux ---
            final_pnl = equity - capital
            wins = [t for t in trades if t.get('pnl', 0) > 0]
            win_rate = (len(wins) / len(trades)) * 100 if trades else 0
            
            equity_series = pd.Series(equity_curve)
            peak = equity_series.expanding(min_periods=1).max()
            drawdown = ((equity_series - peak) / peak).min() if not peak.empty else 0

            results = {
                "pnl": final_pnl, "total_trades": len(trades), "win_rate": win_rate,
                "max_drawdown_percent": abs(drawdown * 100), "equity_curve": equity_curve,
            }
            self.state.finish_backtest(results)
            self.log.info(f"Backtest terminé. PnL final: {final_pnl:.2f}$")

        except Exception as e:
            self.log.error(f"Erreur durant le backtest: {e}", exc_info=True)
            self.state.finish_backtest({"error": str(e)})

def load_yaml(filepath: str) -> dict:
    with open(filepath, 'r', encoding='utf-8') as f:
        return yaml.safe_load(f)