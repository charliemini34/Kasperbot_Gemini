# Fichier: src/backtest/backtester.py
# Version: 2.2.0 (TopDown Adapt - Strict No Interface Params)
# Dépendances: pandas, MetaTrader5, logging, datetime, pytz
# DESCRIPTION: Adapte backtester pour Top-Down, lit TFs depuis config (pas d'override API).

import pandas as pd
import MetaTrader5 as mt5
import logging
from datetime import datetime, timedelta
import pytz
import math
import time

# Importer les composants nécessaires (ajuster les chemins si nécessaire)
# Assurez-vous que ces imports correspondent à la structure de votre projet
try:
    from src.patterns.pattern_detector import PatternDetector
    from src.risk.risk_manager import RiskManager
    from src.constants import BUY, SELL
except ImportError:
    # Fallback si lancé depuis un autre répertoire (ex: racine du projet)
    import sys
    import os
    sys.path.append(os.path.join(os.path.dirname(__file__), '..', '..')) # Ajoute la racine
    from src.patterns.pattern_detector import PatternDetector
    from src.risk.risk_manager import RiskManager
    from src.constants import BUY, SELL


# Classe MockConnector (pour simuler les appels de données MT5)
class MockConnector:
    def __init__(self, htf_data: pd.DataFrame, ltf_data: pd.DataFrame, htf_tf: str, ltf_tf: str):
        self._htf_data = htf_data
        self._ltf_data = ltf_data
        self._htf_tf = htf_tf
        self._ltf_tf = ltf_tf
        self.log = logging.getLogger(self.__class__.__name__)

    def get_ohlc(self, symbol: str, timeframe: str, count: int, current_time=None):
        """ Simule get_ohlc en retournant une slice des données préchargées. """
        if not current_time:
             self.log.error("MockConnector.get_ohlc needs current_time for backtest.")
             return pd.DataFrame() # Retourner DF vide en cas d'erreur

        # Utiliser current_time pour slicer les données historiques
        if timeframe == self._htf_tf:
            # S'assurer que l'index est trié (peut éviter des erreurs de slicing)
            if not self._htf_data.index.is_monotonic_increasing:
                self._htf_data.sort_index(inplace=True)
            data = self._htf_data[self._htf_data.index <= current_time].tail(count)
            return data.copy() # Retourner une copie
        elif timeframe == self._ltf_tf:
            if not self._ltf_data.index.is_monotonic_increasing:
                self._ltf_data.sort_index(inplace=True)
            data = self._ltf_data[self._ltf_data.index <= current_time].tail(count)
            return data.copy()
        else:
            self.log.warning(f"MockConnector: Timeframe {timeframe} not handled (expects {self._htf_tf} or {self._ltf_tf})")
            return pd.DataFrame()

    # Ajouter d'autres méthodes mockées si PatternDetector ou RiskManager les utilisent
    # def get_tick(self, symbol): ... (pourrait retourner la clôture LTF)

class Backtester:
    """ Effectue un backtest de la stratégie Top-Down en utilisant la config fournie. """
    def __init__(self, config: dict, symbol: str, start_date: str, end_date: str, initial_capital: float, state=None):
        self.log = logging.getLogger(self.__class__.__name__)
        self.config = config # Utilise la config passée en argument
        self.symbol = symbol
        self.start_date = start_date
        self.end_date = end_date
        self.initial_capital = initial_capital
        self.state = state # Pour reporter la progression via API

        # Lit les timeframes directement depuis la config fournie
        self.htf_timeframe = config.get('trend_filter', {}).get('higher_timeframe', 'H4')
        self.ltf_timeframe = config.get('trading_settings', {}).get('timeframe', 'M15')

        self.tf_mapping = { # Mapping string vers constantes MT5
             'M1': mt5.TIMEFRAME_M1, 'M5': mt5.TIMEFRAME_M5, 'M15': mt5.TIMEFRAME_M15,
             'M30': mt5.TIMEFRAME_M30, 'H1': mt5.TIMEFRAME_H1, 'H4': mt5.TIMEFRAME_H4,
             'D1': mt5.TIMEFRAME_D1, 'W1': mt5.TIMEFRAME_W1, 'MN1': mt5.TIMEFRAME_MN1
        }
        self.htf = self.tf_mapping.get(self.htf_timeframe)
        self.ltf = self.tf_mapping.get(self.ltf_timeframe)
        if not self.htf or not self.ltf:
             raise ValueError(f"Timeframes '{self.htf_timeframe}' ou '{self.ltf_timeframe}' invalides dans config.")

        # Initialise les composants de la stratégie avec la config fournie
        self.detector = PatternDetector(config)
        self.mock_executor = self._create_mock_executor() # Crée un executor simplifié

        self.htf_data = None # Données HTF chargées
        self.ltf_data = None # Données LTF chargées
        self.results = [] # Liste pour stocker les trades fermés
        self.equity = initial_capital # Équité flottante
        self.balance = initial_capital # Solde après clôture des trades
        self.open_trades = [] # Liste des trades ouverts simulés

    def _create_mock_executor(self):
        """ Crée un executor simplifié pour simuler les infos compte/symbole. """
        class MockExecutor:
             # Classes internes pour simuler les objets retournés par MT5
             class MockSymbolInfo:
                  def __init__(self, point, digits, trade_stops_level, volume_min, volume_max, volume_step, trade_contract_size, currency_profit):
                      self.point, self.digits, self.trade_stops_level = point, digits, trade_stops_level
                      self.volume_min, self.volume_max, self.volume_step = volume_min, volume_max, volume_step
                      self.trade_contract_size, self.currency_profit = trade_contract_size, currency_profit
             class MockAccountInfo:
                   def __init__(self, equity, balance, currency): self.equity, self.balance, self.currency = equity, balance, currency

             def __init__(self_exec, symbol, backtester_instance):
                  self_exec.bt = backtester_instance # Référence au backtester pour equity/balance
                  self_exec.log = logging.getLogger("MockExecutor")
                  # Tente de récupérer les infos réelles au démarrage si MT5 est connecté
                  try:
                      if not mt5.terminal_info(): # Vérifier si MT5 est connecté
                          raise ConnectionError("MT5 non connecté pour récupérer infos réelles.")
                      info = mt5.symbol_info(symbol)
                      acc_info = mt5.account_info()
                      if not info or not acc_info: raise ValueError("Infos MT5 non récupérées.")
                      self_exec.symbol_info_real = MockExecutor.MockSymbolInfo(info.point, info.digits, info.trade_stops_level, info.volume_min, info.volume_max, info.volume_step, info.trade_contract_size, info.currency_profit)
                      self_exec.account_currency = acc_info.currency
                      self_exec.log.info(f"Infos réelles MT5 chargées pour {symbol} / Compte {acc_info.login}")
                  except (Exception, ConnectionError, ValueError) as e:
                       self_exec.log.warning(f"Infos MT5 réelles indisponibles ({e}). Utilisation valeurs par défaut pour MockExecutor.")
                       # Valeurs par défaut génériques (peuvent nécessiter ajustement)
                       point = 0.00001 if "JPY" not in symbol else 0.001
                       digits = 5 if "JPY" not in symbol else 3
                       self_exec.symbol_info_real = MockExecutor.MockSymbolInfo(point, digits, 10, 0.01, 100.0, 0.01, 100000, symbol[3:] if len(symbol)>=6 else "USD")
                       self_exec.account_currency = "EUR" # Supposer EUR par défaut

             def symbol_info(self_exec, symbol_arg): return self_exec.symbol_info_real # Retourne toujours les infos stockées
             def account_info(self_exec): return MockExecutor.MockAccountInfo(self_exec.bt.equity, self_exec.bt.balance, self_exec.account_currency) # Retourne état actuel BT
             @property
             def _mt5(self_exec): # Propriété pour que RiskManager accède via self._executor._mt5
                  class MockMT5: # Simule l'objet mt5 pour RiskManager
                       def symbol_info(self, s): return self_exec.symbol_info(s)
                       def account_info(self): return self_exec.account_info()
                       def symbol_info_tick(self, pair): # Simulation très basique pour conversion
                           # Taux 1:1 par défaut si conversion nécessaire
                           self_exec.log.debug(f"Mock MT5: symbol_info_tick({pair}) appelé, retourne Ask=1.0, Bid=1.0")
                           return type('obj', (), {'ask': 1.0, 'bid': 1.0})
                  return MockMT5()
        return MockExecutor(self.symbol, self)

    def _load_data(self):
        """ Charge les données HTF et LTF depuis MT5 pour la période. """
        self.log.info(f"Chargement données {self.symbol} [{self.htf_timeframe}/{self.ltf_timeframe}] de {self.start_date} à {self.end_date}...")
        try:
            start_dt = pytz.utc.localize(datetime.strptime(self.start_date, '%Y-%m-%d'))
            # Ajouter 1 jour et retirer 1 seconde pour inclure toute la journée de fin
            end_dt = pytz.utc.localize(datetime.strptime(self.end_date, '%Y-%m-%d')) + timedelta(days=1) - timedelta(seconds=1)

            # Charger HTF
            htf_rates = mt5.copy_rates_range(self.symbol, self.htf, start_dt, end_dt)
            if htf_rates is None or len(htf_rates) == 0: raise ValueError(f"Aucune donnée HTF ({self.htf_timeframe}).")
            self.htf_data = pd.DataFrame(htf_rates); self.htf_data['time'] = pd.to_datetime(self.htf_data['time'], unit='s', utc=True); self.htf_data.set_index('time', inplace=True)
            self.htf_data = self.htf_data[['open', 'high', 'low', 'close', 'tick_volume']].rename(columns=str.lower).rename(columns={'tick_volume':'volume'})

            # Charger LTF
            ltf_rates = mt5.copy_rates_range(self.symbol, self.ltf, start_dt, end_dt)
            if ltf_rates is None or len(ltf_rates) == 0: raise ValueError(f"Aucune donnée LTF ({self.ltf_timeframe}).")
            self.ltf_data = pd.DataFrame(ltf_rates); self.ltf_data['time'] = pd.to_datetime(self.ltf_data['time'], unit='s', utc=True); self.ltf_data.set_index('time', inplace=True)
            self.ltf_data = self.ltf_data[['open', 'high', 'low', 'close', 'tick_volume']].rename(columns=str.lower).rename(columns={'tick_volume':'volume'})

            self.log.info(f"Données chargées: {len(self.htf_data)} HTF, {len(self.ltf_data)} LTF.")
            if self.ltf_data.empty: raise ValueError("Données LTF vides.")

        except Exception as e:
            self.log.error(f"Erreur chargement données MT5: {e}", exc_info=True); return False
        return True

    def run(self):
        """ Exécute la boucle principale du backtest. """
        start_time_bt = time.time()
        # Initialiser MT5 (nécessaire pour _load_data et _create_mock_executor)
        mt5_initialized = mt5.initialize()
        if not mt5_initialized:
            self.log.error("Échec initialisation MT5 pour backtest.")
            if self.state: self.state.update_backtest_status("Erreur MT5 Init", 100)
            return None # Impossible de continuer sans MT5 pour les données/infos

        # Vérifier si le symbole existe dans MT5
        symbol_info_check = mt5.symbol_info(self.symbol)
        if not symbol_info_check:
             self.log.error(f"Symbole {self.symbol} non trouvé sur la plateforme MT5.")
             mt5.shutdown()
             if self.state: self.state.update_backtest_status(f"Erreur Symbole {self.symbol}", 100)
             return None
        # Sélectionner le symbole (bonne pratique)
        if not mt5.symbol_select(self.symbol, True):
            self.log.warning(f"Impossible de sélectionner {self.symbol} dans MarketWatch (déjà présent?).")
            # Ne pas arrêter, mais logguer

        # Charger les données historiques
        if not self._load_data():
            mt5.shutdown()
            if self.state: self.state.update_backtest_status("Erreur chargement données", 100)
            return None # Arrêter si les données ne peuvent être chargées

        # Initialiser le MockConnector avec les données chargées
        mock_connector = MockConnector(self.htf_data, self.ltf_data, self.htf_timeframe, self.ltf_timeframe)

        # Initialiser RiskManager avec le MockExecutor (utilise self.config)
        try:
             risk_manager = RiskManager(self.config, self.mock_executor, self.symbol)
        except ValueError as e:
             self.log.error(f"Erreur initialisation RiskManager: {e}")
             mt5.shutdown() # Fermer MT5 si RM échoue
             return None

        total_candles = len(self.ltf_data); processed_candles = 0
        self.log.info(f"Début de la simulation sur {total_candles} bougies LTF...")

        # --- Boucle Principale du Backtest ---
        for timestamp, ltf_candle in self.ltf_data.iterrows():
            processed_candles += 1
            current_time_utc = timestamp # Timestamp de la bougie LTF actuelle

            # Préparer les slices de données pour cette itération
            ltf_slice = self.ltf_data[self.ltf_data.index <= current_time_utc]
            # Assurer que l'index HTF est correct pour le slicing
            htf_slice = self.htf_data[self.htf_data.index <= current_time_utc]

            # Vérifier si assez de données pour l'analyse
            # TODO: Ajuster ces nombres si PatternDetector ou RiskManager changent leurs exigences
            min_ltf_bars = 50
            min_htf_bars = max(210, self.config.get('trend_filter',{}).get('ema_slow_period', 200) + 10) # Assez pour EMA lente
            if len(ltf_slice) < min_ltf_bars or len(htf_slice) < min_htf_bars:
                # Pas assez de données historiques au début, on saute
                continue

            # 1. Gérer les trades ouverts (SL/TP) sur la bougie LTF actuelle
            self._manage_open_trades(ltf_candle, risk_manager)

            # 2. Vérifier nouveau signal avec la stratégie Top-Down
            try:
                # PatternDetector utilise maintenant le MockConnector
                trade_signal = self.detector.detect_patterns(htf_slice, ltf_slice, mock_connector, self.symbol)
            except Exception as e_detect:
                 # Capturer les erreurs potentielles dans detect_patterns
                 self.log.error(f"Erreur dans detect_patterns @ {current_time_utc}: {e_detect}", exc_info=True)
                 trade_signal = None # Pas de signal si erreur

            # 3. Exécuter nouveau trade si signal valide et limite non atteinte
            if trade_signal and len(self.open_trades) < self.config.get('risk_management', {}).get('max_concurrent_trades', 5):
                self._process_signal(trade_signal, risk_manager, ltf_slice, ltf_candle)

            # Mettre à jour la progression pour l'interface utilisateur
            if processed_candles % 200 == 0 and self.state: # Maj moins fréquente
                progress = int((processed_candles / total_candles) * 100)
                # Limiter le message pour éviter surcharge UI
                status_msg = f"Traitement {processed_candles}/{total_candles}"
                if len(status_msg) > 50: status_msg = f"Prog. {progress}%"
                self.state.update_backtest_status(status_msg, progress)

            # Mettre à jour l'équité flottante à chaque bougie
            self._update_equity(ltf_candle['close'])
        # --- Fin Boucle Principale ---

        # Fermer les trades restants à la fin
        if not self.ltf_data.empty:
             self._close_remaining_trades(self.ltf_data.iloc[-1])
        
        # Fermer la connexion MT5 utilisée pour les données
        mt5.shutdown()
        
        duration = time.time() - start_time_bt
        self.log.info(f"Backtest terminé en {duration:.2f} secondes. {len(self.results)} trades exécutés.")
        if self.state: self.state.update_backtest_status(f"Terminé ({len(self.results)} trades)", 100)

        return self._generate_report()

    def _process_signal(self, trade_signal, risk_manager, ltf_data_slice, current_ltf_candle):
        """ Tente d'ouvrir un trade basé sur le signal, calcule params. """
        entry_price = current_ltf_candle['close'] # Simule entrée à la clôture LTF
        direction = trade_signal['direction']
        mock_account_info = self.mock_executor.account_info() # Récupère équité/solde simulés
        try:
             volume, sl, tp = risk_manager.calculate_trade_parameters(
                 mock_account_info.equity, entry_price,
                 ltf_data_slice, # RM utilise LTF pour SL/ATR
                 trade_signal # Contient target_price HTF
             )
        except Exception as e_calc:
             self.log.error(f"Erreur calculate_trade_parameters @ {current_ltf_candle.name}: {e_calc}", exc_info=True)
             volume = 0.0; sl = 0.0; tp = 0.0

        if volume > 0 and sl != 0 and tp != 0: # Vérifier validité SL/TP aussi
             self._open_trade(trade_signal, entry_price, volume, sl, tp, current_ltf_candle.name)
        elif volume <= 0:
             # Log déjà présent dans calculate_trade_parameters si RR faible ou vol < min etc.
             pass

    def _open_trade(self, signal, entry_price, volume, sl, tp, open_time):
        """ Simule l'ouverture d'un trade et l'ajoute à self.open_trades. """
        trade_id = f"BT-{len(self.results)+1}-{int(open_time.timestamp())}"
        new_trade = {
            'trade_id': trade_id, 'symbol': self.symbol, 'direction': signal['direction'],
            'pattern': signal['pattern'], 'volume': volume, 'entry_price': entry_price,
            'sl': sl, 'tp': tp, 'open_time': open_time,
            'close_time': None, 'close_price': None, 'pnl': 0.0, 'status': 'open', 'reason': ''
        }
        self.open_trades.append(new_trade)
        # Log plus concis
        self.log.info(f"OUVERT ({trade_id}): {signal['direction']} {volume:.2f} @{entry_price:.5f} SL={sl:.5f} TP={tp:.5f} | {open_time.strftime('%Y-%m-%d %H:%M')}")

    def _manage_open_trades(self, current_ltf_candle, risk_manager):
        """ Vérifie SL/TP sur la bougie LTF actuelle pour les trades ouverts. """
        candle_high = current_ltf_candle['high']; candle_low = current_ltf_candle['low']
        current_time = current_ltf_candle.name
        trades_to_close = []

        for trade in self.open_trades:
            close_reason = None; close_price = None
            # Logique SL/TP (inchangée)
            if trade['direction'] == BUY:
                if candle_low <= trade['sl']: close_price = trade['sl']; close_reason = "SL"
                elif candle_high >= trade['tp']: close_price = trade['tp']; close_reason = "TP"
            elif trade['direction'] == SELL:
                if candle_high >= trade['sl']: close_price = trade['sl']; close_reason = "SL"
                elif candle_low <= trade['tp']: close_price = trade['tp']; close_reason = "TP"

            if close_reason:
                trades_to_close.append((trade, close_price, current_time, close_reason))
            # else:
                # TODO: Implémenter simulation BE/Trailing si nécessaire
                # mock_tick = {'bid': current_ltf_candle['close'], 'ask': current_ltf_candle['close']} # Approximation
                # risk_manager.manage_open_positions([trade_obj_mt5_like], mock_tick, ltf_data_slice_for_rm)
                # Analyser les actions retournées (modif SL/TP) et les appliquer à `trade` dict.

        # Clôturer les trades marqués
        for trade, price, time, reason in trades_to_close:
            self._close_trade(trade, price, time, reason)

    def _close_trade(self, trade, close_price, close_time, reason=""):
        """ Simule clôture, calcule PNL, met à jour balance, ajoute aux résultats. """
        if trade['status'] != 'open': return # Évite double clôture

        contract_size = self.mock_executor.symbol_info_real.trade_contract_size
        volume = trade['volume']; entry_price = trade['entry_price']
        profit_currency = self.mock_executor.symbol_info_real.currency_profit
        account_currency = self.mock_executor.account_currency
        point = self.mock_executor.symbol_info_real.point

        pnl_points = (close_price - entry_price) if trade['direction'] == BUY else (entry_price - close_price)
        # Convertir points en prix (division par 'point') n'est PAS nécessaire ici
        # pnl_profit_currency = (pnl_points / point) * ... -> Incorrect
        pnl_profit_currency = pnl_points * contract_size * volume

        # Convertir en devise du compte
        pnl_account_currency = pnl_profit_currency
        if profit_currency != account_currency:
             try:
                 # Utiliser une instance RM temporaire juste pour la conversion
                 # Ceci suppose que RiskManager peut être instancié sans erreur ici
                 temp_rm = RiskManager(self.config, self.mock_executor, self.symbol)
                 rate = temp_rm.get_conversion_rate(profit_currency, account_currency)
                 if rate and rate > 0: pnl_account_currency *= rate
                 else: self.log.warning(f"Taux conversion PNL invalide {trade['trade_id']}. Utilise 1.0.")
             except Exception as e_conv: # Capture erreur init RM ou conversion
                 self.log.error(f"Erreur conversion PNL {trade['trade_id']}: {e_conv}. Utilise 1.0.")
                 # pnl_account_currency reste pnl_profit_currency (taux 1.0 implicite)

        # Simuler commission
        commission = self.config.get('backtest_settings', {}).get('commission_per_lot', 0) * volume
        # Simuler spread (optionnel, plus complexe)
        # spread_cost = self.config.get('backtest_settings',{}).get('simulated_spread_pips',0) * point * contract_size * volume * rate_spread

        final_pnl = pnl_account_currency - commission

        trade.update({'close_price': close_price, 'close_time': close_time, 'pnl': final_pnl, 'status': 'closed', 'reason': reason})
        self.balance += final_pnl # Mettre à jour solde
        self._update_equity(close_price) # Recalculer équité après clôture

        self.results.append(trade.copy()) # Sauvegarder copie du trade fermé
        self.open_trades.remove(trade) # Retirer de la liste des ouverts
        # Log plus concis
        self.log.info(f"CLOS ({trade['trade_id']}): {reason} @ {close_price:.5f} PNL={final_pnl:.2f} | Bal={self.balance:.2f} | {close_time.strftime('%Y-%m-%d %H:%M')}")

    def _close_remaining_trades(self, last_candle):
        """ Ferme trades ouverts à la fin du backtest au dernier prix close. """
        close_price = last_candle['close']; close_time = last_candle.name
        if self.open_trades:
             self.log.info(f"Fermeture des {len(self.open_trades)} trade(s) restant(s) à la fin du backtest @ {close_price:.5f}")
             for trade in list(self.open_trades): self._close_trade(trade, close_price, close_time, "Fin Backtest")

    def _update_equity(self, current_price):
        """ Met à jour l'équité flottante basée sur les trades ouverts. """
        current_pnl_floating = 0.0
        contract_size = self.mock_executor.symbol_info_real.trade_contract_size
        profit_currency = self.mock_executor.symbol_info_real.currency_profit
        account_currency = self.mock_executor.account_currency; rate = 1.0
        if profit_currency != account_currency:
             try: temp_rm = RiskManager(self.config, self.mock_executor, self.symbol); rate = temp_rm.get_conversion_rate(profit_currency, account_currency) or 1.0
             except ValueError: rate = 1.0 # Fallback si init RM échoue
        for trade in self.open_trades:
             pnl_points = (current_price - trade['entry_price']) if trade['direction'] == BUY else (trade['entry_price'] - current_price)
             current_pnl_floating += (pnl_points * contract_size * trade['volume']) * rate
        self.equity = self.balance + current_pnl_floating

    def _generate_report(self):
        """ Génère un dictionnaire résumé et la liste des trades pour l'API. """
        if not self.results: return {"summary": "Aucun trade exécuté.", "trades": []}
        df_results = pd.DataFrame(self.results)
        # Calculs statistiques (inchangés)
        total_trades=len(df_results); winning_trades=df_results[df_results['pnl']>0]; losing_trades=df_results[df_results['pnl']<=0]
        win_rate = (len(winning_trades)/total_trades)*100 if total_trades>0 else 0; total_pnl = df_results['pnl'].sum()
        avg_win=winning_trades['pnl'].mean() if not winning_trades.empty else 0; avg_loss=losing_trades['pnl'].mean() if not losing_trades.empty else 0
        rr_ratio=abs(avg_win/avg_loss) if avg_loss!=0 else float('inf'); profit_factor=winning_trades['pnl'].sum()/abs(losing_trades['pnl'].sum()) if abs(losing_trades['pnl'].sum())>0 else float('inf')
        df_results['balance_after_close']=self.initial_capital+df_results['pnl'].cumsum(); df_results['peak']=df_results['balance_after_close'].cummax()
        df_results['drawdown']=df_results['peak']-df_results['balance_after_close']; max_drawdown=df_results['drawdown'].max()
        max_drawdown_pct=(max_drawdown/df_results['peak'].max())*100 if df_results['peak'].max() > 0 else 0
        # Résumé
        summary = {"Period": f"{self.start_date} to {self.end_date}", "Symbol": self.symbol, "Strategy": f"TopDown {self.htf_timeframe}/{self.ltf_timeframe}", "Initial Capital": f"{self.initial_capital:.2f}", "Final Balance": f"{self.balance:.2f}", "Total Net PNL": f"{total_pnl:.2f}", "Total Trades": total_trades, "Win Rate (%)": f"{win_rate:.2f}", "Avg Win": f"{avg_win:.2f}", "Avg Loss": f"{avg_loss:.2f}", "Avg RR Ratio": f"{rr_ratio:.2f}", "Profit Factor": f"{profit_factor:.2f}", "Max Drawdown": f"{max_drawdown:.2f}", "Max Drawdown (%)": f"{max_drawdown_pct:.2f}"}
        # Formater les trades pour JSON
        df_results['open_time'] = df_results['open_time'].dt.strftime('%Y-%m-%d %H:%M:%S')
        df_results['close_time'] = df_results['close_time'].dt.strftime('%Y-%m-%d %H:%M:%S')
        report_trades = df_results[['trade_id', 'open_time', 'close_time', 'symbol', 'direction', 'pattern', 'volume', 'entry_price', 'sl', 'tp', 'close_price', 'pnl', 'reason', 'balance_after_close']].rename(columns={'reason': 'close_reason'})
        return {"summary": summary, "trades": report_trades.to_dict('records')}