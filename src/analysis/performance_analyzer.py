# Fichier: src/analysis/performance_analyzer.py

import pandas as pd
import os
import logging

class PerformanceAnalyzer:
    """
    Analyse l'historique des trades pour fournir des suggestions d'optimisation.
    v1.0 : Analyse par pattern et génération de suggestions.
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
            if len(df) < 10: # Seuil minimum de trades pour une analyse pertinente
                self.log.info(f"Analyse de performance : Pas assez de trades ({len(df)}/10) pour une analyse statistique.")
                return

            self.log.info("--- Début de l'analyse de performance des stratégies ---")
            
            # Analyse par pattern (stratégie)
            results_by_pattern = df.groupby('pattern_trigger').apply(self._calculate_metrics)
            
            suggestions = []
            for pattern, metrics in results_by_pattern.iterrows():
                suggestion = self._generate_suggestion(pattern, metrics)
                if suggestion:
                    suggestions.append(suggestion)
                    self.log.warning(suggestion) # Log en warning pour être plus visible

            self.state.update_analysis_suggestions(suggestions)
            self.log.info("--- Fin de l'analyse de performance ---")

        except Exception as e:
            self.log.error(f"Erreur lors de l'analyse de performance : {e}", exc_info=True)

    def _calculate_metrics(self, group):
        """Calcule les métriques de performance pour un groupe de trades."""
        total_trades = len(group)
        wins = group[group['pnl'] > 0]
        losses = group[group['pnl'] <= 0]
        
        win_rate = (len(wins) / total_trades) * 100 if total_trades > 0 else 0
        
        total_gain = wins['pnl'].sum()
        total_loss = abs(losses['pnl'].sum())
        
        profit_factor = total_gain / total_loss if total_loss > 0 else float('inf')
        
        return pd.Series({
            'total_trades': total_trades,
            'win_rate': win_rate,
            'profit_factor': profit_factor,
            'net_pnl': group['pnl'].sum()
        })

    def _generate_suggestion(self, pattern, metrics):
        """Génère une suggestion textuelle basée sur les métriques."""
        suggestion = f"ANALYSE ({pattern}): {metrics['total_trades']} trades, Taux réussite: {metrics['win_rate']:.1f}%, PnL Net: {metrics['net_pnl']:.2f}$."
        
        # Logique de suggestion
        if metrics['total_trades'] >= 10: # On ne fait de suggestion forte qu'avec assez de données
            if metrics['win_rate'] < 40 and metrics['net_pnl'] < 0:
                suggestion += " SUGGESTION: Performance faible. Envisagez de désactiver ce pattern."
            elif metrics['profit_factor'] > 2.0 and metrics['win_rate'] > 55:
                suggestion += " INFO: Très bonne performance."

        return suggestion