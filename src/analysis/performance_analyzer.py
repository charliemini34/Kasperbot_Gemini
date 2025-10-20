# Fichier: src/analysis/performance_analyzer.py
# Version: 3.1.2 (AI-Prompt-Syntax-Fix-Final)
# Dépendances: pandas, os, logging, numpy, yaml
# Description: Corrige DÉFINITIVEMENT l'erreur de syntaxe dans la f-string du prompt IA.

import pandas as pd
import os
import logging
import numpy as np
import yaml

class PerformanceAnalyzer:
    """ Analyse statistique simple de l'historique des trades. """
    def __init__(self, state):
        self.state = state
        self.log = logging.getLogger(self.__class__.__name__)
        self.history_file = 'trade_history.csv'

    def run_analysis(self):
        """Lance l'analyse statistique simple (pour logs/debug)."""
        if not os.path.exists(self.history_file) or os.path.getsize(self.history_file) == 0:
            self.log.info("Analyse Stats: Historique vide.")
            return
        try:
            df = pd.read_csv(self.history_file)
            min_trades = self.state.get_config().get('learning', {}).get('min_trades_for_analysis', 10)
            if len(df) < min_trades:
                self.log.info(f"Analyse Stats: Pas assez de trades ({len(df)}/{min_trades}).")
                return
            self.log.info("--- Début Analyse Statistique Simple ---")
            results_by_context = df.groupby(['pattern_trigger']).apply(self._calculate_metrics)
            suggestions = []
            for pattern, metrics in results_by_context.iterrows():
                suggestion = self._generate_suggestion(pattern, metrics)
                if suggestion:
                    suggestions.append(suggestion)
                    self.log.info(suggestion) # Logue les stats simples
            self.log.info("--- Fin Analyse Statistique Simple ---")
        except Exception as e:
            self.log.error(f"Erreur Analyse Stats : {e}", exc_info=True)

    def _calculate_metrics(self, group):
        """Calcule les métriques de base pour un groupe de trades."""
        total_trades = len(group); wins = group[group['pnl'] > 0]
        win_rate = (len(wins) / total_trades) * 100 if total_trades > 0 else 0
        total_gain = wins['pnl'].sum(); total_loss = abs(group[group['pnl'] <= 0]['pnl'].sum())
        profit_factor = total_gain / total_loss if total_loss > 0 else np.inf
        if profit_factor == np.inf: profit_factor_display = "Inf"
        elif pd.isna(profit_factor): profit_factor_display = "N/A"
        else: profit_factor_display = f"{profit_factor:.2f}"
        return pd.Series({'total_trades': total_trades, 'win_rate': win_rate, 'profit_factor': profit_factor, 'profit_factor_display': profit_factor_display, 'net_pnl': group['pnl'].sum()})

    def _generate_suggestion(self, pattern, metrics):
        """Formate une ligne de suggestion basée sur les métriques simples."""
        suggestion = (f"Stats ({pattern}): {metrics['total_trades']} trades, "
                      f"WR: {metrics['win_rate']:.1f}%, PF: {metrics['profit_factor_display']}, "
                      f"Net: {metrics['net_pnl']:.2f}$")
        if metrics['profit_factor'] < 1.0 and metrics['net_pnl'] < 0:
             suggestion += ". ATTENTION: Perdant."
        return suggestion

class AIAnalyzer:
    """ Prépare un prompt pour analyse IA basé sur l'historique et la config. """
    def __init__(self, config: dict):
        self.log = logging.getLogger(self.__class__.__name__)
        self.config_ai = config.get('ai_analysis', {})
        self.full_config = config
        self.history_file = 'trade_history.csv'
        self.log.info("AIAnalyzer initialisé pour la génération de prompt.")

    def run_ai_analysis(self) -> str:
        """ Lit les données, construit et retourne le prompt pour l'IA. """
        try:
            # Lire historique
            if not os.path.exists(self.history_file): return f"Erreur: Fichier historique '{self.history_file}' introuvable."
            if os.path.getsize(self.history_file) == 0: return "Information: Historique trades vide."
            df_history = pd.read_csv(self.history_file)
            if df_history.empty: return "Information: Historique trades vide après lecture."
            max_trades = self.config_ai.get('max_trades_in_prompt', 50)
            recent_trades_df = df_history.tail(max_trades)
            recent_trades_string = recent_trades_df.to_string(index=False)
            num_trades_used = len(recent_trades_df)

            # Lire config
            try:
                # Utiliser yaml.dump pour obtenir une représentation textuelle propre et complète
                current_config_yaml = yaml.dump(self.full_config, default_flow_style=False, sort_keys=False, allow_unicode=True, width=1000)
            except Exception as e: return f"Erreur: Impossible de formater config ({e})."

            # Construire prompt
            prompt = self._build_prompt(current_config_yaml, recent_trades_string, num_trades_used)
            self.log.info(f"Prompt généré ({len(prompt)} chars, {num_trades_used} trades).")
            return prompt

        except pd.errors.EmptyDataError: return "Information: Historique trades semble vide (erreur pandas)."
        except FileNotFoundError: return f"Erreur: Fichier '{self.history_file}' introuvable (FileNotFound)."
        except Exception as e:
            self.log.error(f"Erreur génération prompt IA : {e}", exc_info=True)
            return f"Erreur interne génération prompt: {e}"

    def _build_prompt(self, config_yaml: str, history_string: str, num_trades: int) -> str:
        """ Construit le prompt détaillé pour l'IA. """
        # --- VÉRIFICATION DE LA PRÉSENCE DES """ FERMANTS ---
        prompt = f"""
**CONTEXTE :**
Tu es un expert en trading algorithmique spécialisé dans les Smart Money Concepts (SMC), avec plus de 20 ans d'expérience. Tu es également ingénieur logiciel senior Python.
Analyse la configuration (`config.yaml`) et l'historique récent des trades (`trade_history.csv`) d'un bot de trading SMC écrit en Python que tu as contribué à développer.
L'objectif est d'identifier des pistes d'optimisation **quantifiables** pour améliorer la rentabilité nette et réduire le drawdown maximal, tout en respectant la logique SMC implémentée.
"""
