import pandas as pd
import os
import logging
import yaml
import shutil
from datetime import datetime
from .ai_assistant import AIAssistant

# Copié depuis server.py pour éviter une dépendance circulaire
DEFAULT_PROFILES = {
    'custom': {'TREND': 0.20, 'SMC': 0.20, 'MEAN_REV': 0.20, 'VOL_BRK': 0.20, 'LONDON_BRK': 0.20}
}

class PerformanceAnalyzer:
    def __init__(self, history_file):
        self.history_file = history_file
        self.log = logging.getLogger(self.__class__.__name__)
        self.ai_assistant = AIAssistant()
        self.open_trades_context = {}
        if not os.path.exists(self.history_file):
            pd.DataFrame(columns=['ticket', 'symbol', 'direction', 'open_time', 'close_time', 'pnl', 'score_TREND', 'score_MEAN_REV', 'score_SMC', 'score_VOL_BRK', 'score_LONDON_BRK']).to_csv(self.history_file, index=False)

    def log_trade_open(self, ticket, symbol, direction, open_time, raw_scores):
        self.open_trades_context[ticket] = {'symbol': symbol, 'direction': direction, 'open_time': open_time, 'raw_scores': raw_scores}

    def log_trade_close(self, ticket, pnl, close_time):
        if ticket not in self.open_trades_context: return
        context = self.open_trades_context.pop(ticket)
        trade_data = {
            'ticket': ticket, 'symbol': context['symbol'], 'direction': context['direction'],
            'open_time': context['open_time'].strftime('%Y-%m-%d %H:%M:%S'),
            'close_time': close_time.strftime('%Y-%m-%d %H:%M:%S'), 'pnl': pnl,
            **{f"score_{k}": v.get('score', 0) for k, v in context['raw_scores'].items() if k in DEFAULT_PROFILES['custom']}
        }
        pd.DataFrame([trade_data]).to_csv(self.history_file, mode='a', header=False, index=False)
        self.log.info(f"Trade #{ticket} (PnL: {pnl:.2f}) ajouté à l'historique.")
        if pnl < 0: self.ai_assistant.get_gemini_analysis(trade_data)

    def run_analysis(self):
        try:
            with open('config.yaml', 'r') as f: config = yaml.safe_load(f)
            df = pd.read_csv(self.history_file)
            if len(df) < 10:
                self.log.info(f"Pas assez de trades ({len(df)}) pour une analyse pertinente.")
                return
            
            self.log.info("--- RAPPORT DE PERFORMANCE DES STRATÉGIES ---")
            performance = {}
            for strat in DEFAULT_PROFILES['custom'].keys():
                relevant = df[df[f'score_{strat}'] > 50]
                if len(relevant) < 3: continue
                win_rate = (len(relevant[relevant['pnl'] > 0]) / len(relevant)) * 100
                performance[strat] = win_rate
                self.log.info(f"Stratégie [{strat}]: Taux de réussite (quand active) = {win_rate:.1f}% sur {len(relevant)} trades.")
            
            if config.get('learning', {}).get('auto_optimization_enabled', False):
                self.log.warning("Mode auto-optimisation activé. Tentative d'ajustement des poids...")
                self._optimize_weights(performance)
            else:
                self.log.info("Mode auto-optimisation désactivé.")
            self.log.info("--- FIN DU RAPPORT ---")
        except Exception as e:
            self.log.error(f"Erreur lors de l'analyse de performance: {e}", exc_info=True)

    def _optimize_weights(self, performance: dict):
        if not performance:
            self.log.info("Aucune donnée de performance pour l'optimisation.")
            return
            
        try:
            with open('profiles.yaml', 'r') as f: profiles = yaml.safe_load(f)
            
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            shutil.copy('profiles.yaml', f'profiles_{timestamp}.yaml.bak')

            custom_weights = profiles.get('custom', DEFAULT_PROFILES['custom'].copy())
            
            total_performance_points = sum(performance.values())
            if total_performance_points == 0:
                self.log.info("Performance nulle, aucun ajustement possible.")
                return

            redistribution_pool = 0.10
            total_weight = sum(custom_weights.values())
            
            for strat, weight in custom_weights.items():
                if strat in performance:
                    perf_share = performance[strat] / total_performance_points
                    target_weight = (weight * (1 - redistribution_pool)) + (perf_share * redistribution_pool * total_weight)
                    custom_weights[strat] = target_weight
            
            final_total = sum(custom_weights.values())
            if final_total > 0:
                custom_weights = {k: round(v / final_total, 4) for k, v in custom_weights.items()}
                
                self.log.warning("Nouveaux poids optimisés pour le profil 'custom':")
                for strat, weight in custom_weights.items():
                    self.log.warning(f"  - {strat}: {weight:.4f}")

                profiles['custom'] = custom_weights
                with open('profiles.yaml', 'w') as f:
                    yaml.dump(profiles, f, default_flow_style=False, sort_keys=False)
                
                self.log.info("Les poids du profil 'custom' ont été mis à jour et sauvegardés.")
            else:
                self.log.error("Erreur de normalisation, la somme des poids est nulle.")

        except Exception as e:
            self.log.error(f"Erreur lors de l'optimisation des poids: {e}", exc_info=True)