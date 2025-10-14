# Fichier: src/analysis/performance_analyzer.py

import pandas as pd
import os
import logging
import yaml
import shutil
from datetime import datetime

class PerformanceAnalyzer:
    def __init__(self, history_file):
        self.history_file = history_file
        self.log = logging.getLogger(self.__class__.__name__)
        self.open_trades_context = {}
        
        self.columns = [
            'ticket', 'symbol', 'direction', 'open_time', 'close_time', 'pnl', 
            'final_score', 'dominant_strategy', 'score_TREND', 'score_MEAN_REV', 
            'score_SMC', 'score_VOL_BRK', 'score_LONDON_BRK', 'score_INBALANCE'
        ]

        if not os.path.exists(self.history_file):
            pd.DataFrame(columns=self.columns).to_csv(self.history_file, index=False)

    def log_trade_open(self, ticket, symbol, direction, open_time, final_score, raw_scores):
        """Enregistre le contexte d'un trade à son ouverture."""
        if not raw_scores:
            dominant_strategy = "N/A"
        else:
            dominant_strategy = max(raw_scores, key=lambda k: raw_scores.get(k, {}).get('score', 0))
        
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
            'ticket': ticket, 'symbol': context['symbol'], 'direction': context['direction'],
            'open_time': context['open_time'].strftime('%Y-%m-%d %H:%M:%S'),
            'close_time': close_time.strftime('%Y-%m-%d %H:%M:%S'), 'pnl': pnl,
            'final_score': context.get('final_score', 0),
            'dominant_strategy': context.get('dominant_strategy', 'N/A'),
            **{f"score_{k}": v.get('score', 0) for k, v in context.get('raw_scores', {}).items()}
        }
        
        df_row = pd.DataFrame([trade_data], columns=self.columns)
        df_row.to_csv(self.history_file, mode='a', header=False, index=False)
        self.log.info(f"Trade #{ticket} (PnL: {pnl:.2f}) ajouté à l'historique.")

    def run_analysis(self):
        """Analyse l'historique des trades et déclenche l'optimisation des poids si activée."""
        try:
            with open('config.yaml', 'r') as f: config = yaml.safe_load(f)
            df = pd.read_csv(self.history_file)
            if len(df) < 10:
                self.log.warning(f"Analyse des performances reportée : {len(df)}/10 trades enregistrés.")
                return
            
            self.log.info("--- RAPPORT DE PERFORMANCE DES STRATÉGIES ---")
            performance = {}
            for strat in self.get_strategy_names_from_profiles():
                relevant_trades = df[df['dominant_strategy'] == strat]
                if len(relevant_trades) < 3: continue
                win_rate = (len(relevant_trades[relevant_trades['pnl'] > 0]) / len(relevant_trades)) * 100
                performance[strat] = {'win_rate': win_rate, 'trade_count': len(relevant_trades)}
                self.log.info(f"Stratégie [{strat}]: Taux de réussite = {win_rate:.1f}% sur {len(relevant_trades)} trades.")
            
            if config.get('learning', {}).get('auto_optimization_enabled', False):
                self.log.warning("Mode auto-optimisation activé. Tentative d'ajustement des poids...")
                self._optimize_weights(performance, config)
            
            self.log.info("--- FIN DU RAPPORT ---")
        except Exception as e:
            self.log.error(f"Erreur lors de l'analyse de performance: {e}", exc_info=True)
            
    def get_strategy_names_from_profiles(self):
        """Lit profiles.yaml pour obtenir la liste de toutes les stratégies."""
        try:
            with open('profiles.yaml', 'r') as f:
                profiles = yaml.safe_load(f)
            return list(profiles.get('custom', {}).keys())
        except:
            return ['TREND', 'SMC', 'MEAN_REV', 'VOL_BRK', 'LONDON_BRK', 'INBALANCE']

    def _optimize_weights(self, performance: dict, config: dict):
        """Ajuste les poids du profil 'custom' en fonction des performances."""
        if not performance:
            self.log.info("Aucune donnée de performance pour l'optimisation.")
            return
            
        try:
            with open('profiles.yaml', 'r') as f: profiles = yaml.safe_load(f)
            
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            shutil.copy('profiles.yaml', f'profiles_{timestamp}.yaml.bak')

            custom_weights = profiles.get('custom', {}).copy()
            learning_rate = config.get('learning', {}).get('learning_rate', 0.05)
            
            # Identifier la meilleure et la pire stratégie
            best_strat = max(performance, key=lambda k: performance[k]['win_rate'])
            worst_strat = min(performance, key=lambda k: performance[k]['win_rate'])
            
            if best_strat != worst_strat:
                adjustment = custom_weights[worst_strat] * learning_rate
                
                # Renforcer la meilleure, affaiblir la pire
                custom_weights[best_strat] += adjustment
                custom_weights[worst_strat] -= adjustment
                
                # Normaliser pour que la somme reste à 1.0
                total_weight = sum(custom_weights.values())
                custom_weights = {k: v / total_weight for k, v in custom_weights.items()}

                self.log.warning("Nouveaux poids optimisés pour le profil 'custom':")
                for strat, weight in custom_weights.items():
                    self.log.warning(f"  - {strat}: {weight:.4f}")

                profiles['custom'] = custom_weights
                with open('profiles.yaml', 'w') as f:
                    yaml.dump(profiles, f, default_flow_style=False, sort_keys=False)
                self.log.info("Les poids du profil 'custom' ont été mis à jour et sauvegardés.")
            else:
                self.log.info("Performances similaires, aucun ajustement des poids n'est nécessaire.")

        except Exception as e:
            self.log.error(f"Erreur lors de l'optimisation des poids: {e}", exc_info=True)