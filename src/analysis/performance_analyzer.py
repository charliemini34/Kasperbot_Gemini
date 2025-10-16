# Fichier: src/analysis/performance_analyzer.py
# Version améliorée par votre Partenaire de Code

import pandas as pd
import os
import logging
import numpy as np

class PerformanceAnalyzer:
    """
    Analyse l'historique des trades pour fournir des suggestions d'optimisation.
    v1.1 : Analyse croisée par pattern et par contexte de marché (tendance).
    """
    def __init__(self, state):
        self.state = state
        self.log = logging.getLogger(self.__class__.__name__)
        self.history_file = 'trade_history.csv'

    def run_analysis(self):
        """Lance le processus complet d'analyse et de suggestion."""
        if not os.path.exists(self.history_file) or os.path.getsize(self.history_file) == 0:
            self.log.info("Analyse de performance : L'historique des trades est vide.")
            return

        try:
            df = pd.read_csv(self.history_file)
            min_trades = self.state.get_config().get('learning', {}).get('min_trades_for_analysis', 10)

            if len(df) < min_trades:
                self.log.info(f"Analyse de performance : Pas assez de trades ({len(df)}/{min_trades}) pour une analyse statistique.")
                return

            self.log.info("--- Début de l'analyse de performance des stratégies ---")
            
            # NOUVEAU : Analyse croisée par pattern ET par tendance de marché
            results_by_context = df.groupby(['pattern_trigger', 'market_trend']).apply(self._calculate_metrics)
            
            suggestions = []
            for (pattern, trend), metrics in results_by_context.iterrows():
                suggestion = self._generate_suggestion(pattern, trend, metrics, min_trades)
                if suggestion:
                    suggestions.append(suggestion)
                    self.log.warning(suggestion)

            self.state.update_analysis_suggestions(suggestions)
            self.log.info("--- Fin de l'analyse de performance ---")

        except Exception as e:
            self.log.error(f"Erreur lors de l'analyse de performance : {e}", exc_info=True)

    def _calculate_metrics(self, group):
        """Calcule les métriques de performance pour un groupe de trades."""
        total_trades = len(group)
        wins = group[group['pnl'] > 0]
        
        win_rate = (len(wins) / total_trades) * 100 if total_trades > 0 else 0
        
        total_gain = wins['pnl'].sum()
        total_loss = abs(group[group['pnl'] <= 0]['pnl'].sum())
        
        profit_factor = total_gain / total_loss if total_loss > 0 else np.inf
        
        return pd.Series({
            'total_trades': total_trades,
            'win_rate': win_rate,
            'profit_factor': profit_factor,
            'net_pnl': group['pnl'].sum()
        })

    def _generate_suggestion(self, pattern, trend, metrics, min_trades):
        """Génère une suggestion textuelle basée sur les métriques et le contexte."""
        # On ne génère une suggestion que si le nombre de trades dans ce contexte est significatif
        if metrics['total_trades'] < (min_trades / 2):
            return None

        suggestion_header = f"ANALYSE ({pattern} / {trend}):"
        suggestion_body = (f" {metrics['total_trades']} trades, "
                           f"Taux réussite: {metrics['win_rate']:.1f}%, "
                           f"PnL Net: {metrics['net_pnl']:.2f}$")
        
        full_suggestion = suggestion_header + suggestion_body

        # Logique de suggestion améliorée
        if metrics['win_rate'] < 40 and metrics['net_pnl'] < 0:
            full_suggestion += ". SUGGESTION: Ce pattern semble peu performant dans ce contexte de marché."
        elif metrics['profit_factor'] > 2.0 and metrics['win_rate'] > 55:
            full_suggestion += ". INFO: Excellente performance de ce pattern dans ce contexte."
        elif metrics['profit_factor'] < 1.0 and metrics['net_pnl'] < 0:
             full_suggestion += ". ATTENTION: Ce pattern est perdant dans ce contexte."

        return full_suggestion