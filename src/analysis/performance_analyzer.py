# Fichier: src/analysis/performance_analyzer.py

import pandas as pd
import os
import logging
import yaml
import shutil
from datetime import datetime
from .ai_assistant import AIAssistant

DEFAULT_PROFILES = {
    'custom': {'TREND': 0.20, 'SMC': 0.20, 'MEAN_REV': 0.20, 'VOL_BRK': 0.20, 'LONDON_BRK': 0.20, 'INBALANCE': 0.20}
}

class PerformanceAnalyzer:
    def __init__(self, history_file):
        self.history_file = history_file
        self.log = logging.getLogger(self.__class__.__name__)
        self.ai_assistant = AIAssistant()
        self.open_trades_context = {}
        
        # NOUVEAU : Colonnes améliorées pour l'historique
        self.columns = [
            'ticket', 'symbol', 'direction', 'open_time', 'close_time', 'pnl', 
            'final_score', 'dominant_strategy', 'score_TREND', 'score_MEAN_REV', 
            'score_SMC', 'score_VOL_BRK', 'score_LONDON_BRK', 'score_INBALANCE'
        ]

        if not os.path.exists(self.history_file):
            pd.DataFrame(columns=self.columns).to_csv(self.history_file, index=False)

    # CORRECTION : Renommage de la fonction
    def log_trade_open(self, ticket, symbol, direction, open_time, final_score, raw_scores):
        """Enregistre le contexte d'un trade à son ouverture."""
        # AMÉLIORATION : Déterminer la stratégie dominante
        dominant_strategy = max(raw_scores, key=lambda k: raw_scores[k]['score'])
        
        self.open_trades_context[ticket] = {
            'symbol': symbol, 'direction': direction, 'open_time': open_time, 
            'final_score': final_score, 'raw_scores': raw_scores,
            'dominant_strategy': dominant_strategy
        }
        self.log.info(f"Contexte du trade #{ticket} enregistré. Stratégie dominante: {dominant_strategy}")

    def log_trade_close(self, ticket, pnl, close_time):
        """Enregistre les détails d'un trade à sa clôture."""
        if ticket not in self.open_trades_context: return
        context = self.open_trades_context.pop(ticket)
        
        trade_data = {
            'ticket': ticket, 
            'symbol': context['symbol'], 
            'direction': context['direction'],
            'open_time': context['open_time'].strftime('%Y-%m-%d %H:%M:%S'),
            'close_time': close_time.strftime('%Y-%m-%d %H:%M:%S'), 
            'pnl': pnl,
            'final_score': context.get('final_score', 0),
            'dominant_strategy': context.get('dominant_strategy', 'N/A'),
            **{f"score_{k}": v.get('score', 0) for k, v in context['raw_scores'].items()}
        }
        
        # S'assurer que toutes les colonnes sont présentes
        df_row = pd.DataFrame([trade_data], columns=self.columns)
        df_row.to_csv(self.history_file, mode='a', header=False, index=False)
        
        self.log.info(f"Trade #{ticket} (PnL: {pnl:.2f}) ajouté à l'historique.")
        if pnl < 0: 
            self.ai_assistant.get_gemini_analysis(trade_data)

    def run_analysis(self):
        # ... (Cette fonction reste inchangée pour le moment, mais bénéficiera des nouvelles données)
        pass

    def _optimize_weights(self, performance: dict):
        # ... (Cette fonction reste inchangée)
        pass