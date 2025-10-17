# Fichier: src/analysis/performance_analyzer.py
# Version: 2.0.0 (Learning-Engine-Foundation)

import pandas as pd
import os
import logging
import numpy as np

class PerformanceAnalyzer:
    """
    Analyse l'historique des trades et propose des optimisations de paramètres.
    v2.0: Capacité à suggérer des ajustements de paramètres concrets.
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
            
            results_by_context = df.groupby(['pattern_trigger']).apply(self._calculate_metrics)
            
            suggestions = []
            for pattern, metrics in results_by_context.iterrows():
                suggestion = self._generate_suggestion(pattern, metrics)
                if suggestion:
                    suggestions.append(suggestion)
                    self.log.warning(suggestion)
            
            # --- NOUVEAUTÉ : Logique d'optimisation ---
            optimization_suggestion = self.propose_parameter_optimizations(df)
            if optimization_suggestion:
                suggestions.append(optimization_suggestion)
                self.log.critical(optimization_suggestion) # Log en CRITICAL pour attirer l'attention

            self.state.update_analysis_suggestions(suggestions)
            self.log.info("--- Fin de l'analyse de performance ---")

        except Exception as e:
            self.log.error(f"Erreur lors de l'analyse de performance : {e}", exc_info=True)

    def _calculate_metrics(self, group):
        # ... (Fonction inchangée)
        total_trades = len(group)
        wins = group[group['pnl'] > 0]
        win_rate = (len(wins) / total_trades) * 100 if total_trades > 0 else 0
        total_gain = wins['pnl'].sum()
        total_loss = abs(group[group['pnl'] <= 0]['pnl'].sum())
        profit_factor = total_gain / total_loss if total_loss > 0 else np.inf
        return pd.Series({
            'total_trades': total_trades, 'win_rate': win_rate,
            'profit_factor': profit_factor, 'net_pnl': group['pnl'].sum()
        })

    def _generate_suggestion(self, pattern, metrics):
        # ... (Fonction légèrement simplifiée pour se concentrer sur la performance globale)
        suggestion_header = f"ANALYSE ({pattern}):"
        suggestion_body = (f" {metrics['total_trades']} trades, "
                           f"Taux réussite: {metrics['win_rate']:.1f}%, "
                           f"PnL Net: {metrics['net_pnl']:.2f}$")
        full_suggestion = suggestion_header + suggestion_body
        if metrics['profit_factor'] < 1.0 and metrics['net_pnl'] < 0:
             full_suggestion += ". ATTENTION: Ce pattern est globalement perdant."
        return full_suggestion

    def propose_parameter_optimizations(self, df: pd.DataFrame) -> str or None:
        """Analyse les trades perdants et propose des ajustements de paramètres."""
        losing_trades = df[df['pnl'] < 0]
        if len(losing_trades) < 5:
            return None # Pas assez de données sur les pertes pour une conclusion

        # Exemple : Analyser si les stop-loss sont touchés trop souvent pour un pattern donné
        choch_losses = losing_trades[losing_trades['pattern_trigger'] == 'CHANGE_OF_CHARACTER']
        if len(choch_losses) > 3:
            # Hypothèse simplifiée : si un trade est perdant, on suppose que le SL a été touché.
            # Une analyse plus poussée pourrait comparer la perte au risque initial.
            avg_volatility_on_loss = choch_losses['volatility_atr'].mean()
            if avg_volatility_on_loss > 0:
                # Ceci est une suggestion textuelle, la prochaine étape serait de la rendre exploitable
                return (f"OPTIMISATION: Le pattern 'CHANGE_OF_CHARACTER' perd fréquemment avec une volatilité moyenne de {avg_volatility_on_loss:.5f}. "
                        f"SUGGESTION: Envisager d'augmenter le multiple ATR du Stop Loss pour ce pattern dans config.yaml.")
        return None