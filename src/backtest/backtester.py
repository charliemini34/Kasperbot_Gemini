import MetaTrader5 as mt5
import pandas as pd
from datetime import datetime
import logging
import yaml

from src.scorer.strategy_scorer import StrategyScorer
from src.scorer.aggregator import Aggregator

class Backtester:
    def __init__(self, shared_state):
        self.state = shared_state
        self.log = logging.getLogger(self.__class__.__name__)
        
    def run(self, start_date_str, end_date_str, initial_capital):
        self.log.info(f"Démarrage du backtest de {start_date_str} à {end_date_str}...")
        self.state.start_backtest()

        try:
            with open('config.yaml', 'r') as f:
                config = yaml.safe_load(f)
            
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
            
            capital = float(initial_capital)
            equity_curve = [capital]
            trades = []
            open_position = None
            
            scorer = StrategyScorer()
            aggregator = Aggregator(config['strategy_weights'])
            
            total_bars = len(df)
            for i in range(200, total_bars):
                if i % (total_bars // 100 or 1) == 0:
                    self.state.update_backtest_progress((i / total_bars) * 100)

                current_data = df.iloc[:i]
                current_price_info = df.iloc[i]
                
                if open_position:
                    pnl = 0
                    closed = False
                    if open_position['direction'] == 'BUY':
                        if current_price_info['low'] <= open_position['sl']:
                            pnl = (open_position['sl'] - open_position['entry_price']) * open_position['volume'] * 100
                            closed = True
                        elif current_price_info['high'] >= open_position['tp']:
                            pnl = (open_position['tp'] - open_position['entry_price']) * open_position['volume'] * 100
                            closed = True
                    else: # SELL
                        if current_price_info['high'] >= open_position['sl']:
                            pnl = (open_position['entry_price'] - open_position['sl']) * open_position['volume'] * 100
                            closed = True
                        elif current_price_info['low'] <= open_position['tp']:
                            pnl = (open_position['entry_price'] - open_position['tp']) * open_position['volume'] * 100
                            closed = True
                    
                    if closed:
                        capital += pnl
                        open_position['pnl'] = pnl
                        trades.append(open_position)
                        open_position = None
                        equity_curve.append(capital)
                
                if not open_position:
                    raw_scores = scorer.calculate_all(current_data)
                    final_score, trade_direction = aggregator.calculate_final_score(raw_scores)
                    
                    if final_score >= config['execution_threshold'] and trade_direction != "NEUTRAL":
                        entry_price = current_price_info['close']
                        sl_pips = config['risk_management']['stop_loss_pips']
                        tp_pips = config['risk_management']['take_profit_pips']
                        point = mt5.symbol_info(symbol).point if mt5.symbol_info(symbol) else 0.01
                        
                        sl = entry_price - sl_pips * 10 * point if trade_direction == "BUY" else entry_price + sl_pips * 10 * point
                        tp = entry_price + tp_pips * 10 * point if trade_direction == "BUY" else entry_price - tp_pips * 10 * point
                        
                        open_position = {
                            'direction': trade_direction, 'entry_price': entry_price, 'sl': sl, 'tp': tp,
                            'volume': config['trading_settings']['volume_per_trade'],
                        }

            pnl = equity_curve[-1] - float(initial_capital)
            wins = [t for t in trades if t.get('pnl', 0) > 0]
            win_rate = (len(wins) / len(trades)) * 100 if trades else 0
            
            equity_series = pd.Series(equity_curve)
            peak = equity_series.expanding(min_periods=1).max()
            drawdown = ((equity_series - peak) / peak).min() if not peak.empty else 0

            results = {
                "pnl": pnl, "total_trades": len(trades), "win_rate": win_rate,
                "max_drawdown_percent": abs(drawdown * 100), "equity_curve": equity_curve,
            }

            self.state.finish_backtest(results)
            self.log.info(f"Backtest terminé. PnL final: {pnl:.2f}$")

        except Exception as e:
            self.log.error(f"Erreur durant le backtest: {e}", exc_info=True)
            self.state.finish_backtest({"error": str(e)})