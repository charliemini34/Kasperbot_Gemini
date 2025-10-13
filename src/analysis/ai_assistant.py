import logging
import os
import google.generativeai as genai
from typing import Tuple

class AIAssistant:
    """
    Assistant IA utilisant l'API Gemini pour fournir un second avis sur les trades
    et analyser les performances passées.
    """
    def __init__(self):
        self.log = logging.getLogger(self.__class__.__name__)
        self.model = None
        try:
            api_key = os.getenv("GEMINI_API_KEY")
            if api_key:
                genai.configure(api_key=api_key)
                self.model = genai.GenerativeModel('gemini-pro')
                self.log.info("Assistant IA initialisé avec succès (Gemini-Pro).")
            else:
                self.log.warning("Variable d'environnement GEMINI_API_KEY non trouvée. L'assistant IA sera désactivé.")
        except Exception as e:
            self.log.error(f"Échec de l'initialisation de l'IA Gemini: {e}. L'assistant IA sera désactivé.")
            self.model = None

    def confirm_trade_with_ai(self, trade_signal: dict) -> Tuple[bool, str]:
        """
        Demande à Gemini un second avis sur une opportunité de trade, en agissant comme un gestionnaire de risque.
        """
        if not self.model:
            return True, "IA non configurée, approbation par défaut."

        prompt = f"""
        Role: Tu es un gestionnaire de risque senior pour un hedge fund.
        Tâche: Analyse ce signal de trading automatisé sur XAUUSD. Ta seule mission est de repérer les incohérences et les risques cachés.
        
        Contexte du Signal:
        - Direction proposée : {trade_signal['direction']}
        - Confiance du système : {trade_signal['confidence']:.1f}/100
        
        Scores des stratégies clés (0-100):
        - Tendance (TREND) : {trade_signal['scores'].get('TREND', {}).get('score', 0):.1f}
        - Structure de Marché (SMC) : {trade_signal['scores'].get('SMC', {}).get('score', 0):.1f}
        - Contre-Tendance (MEAN_REV) : {trade_signal['scores'].get('MEAN_REV', {}).get('score', 0):.1f}
        - Cassure de Volatilité (VOL_BRK) : {trade_signal['scores'].get('VOL_BRK', {}).get('score', 0):.1f}

        Règle critique: Sois extrêmement prudent si un signal de TENDANCE fort est accompagné d'un score de CONTRE-TENDANCE (MEAN_REV) élevé (> 40). C'est un drapeau rouge majeur indiquant un conflit.

        Format de réponse obligatoire: Réponds UNIQUEMENT par "APPROUVÉ" ou "REJETÉ", suivi d'une justification de 10 mots maximum.
        
        Ton verdict concis:
        """
        
        try:
            response = self.model.generate_content(prompt)
            # Nettoyage de la réponse pour être plus robuste
            ai_response_text = response.text.strip().upper()
            
            if ai_response_text.startswith("APPROUVÉ"):
                return True, response.text.strip()
            elif ai_response_text.startswith("REJETÉ"):
                return False, response.text.strip()
            else:
                self.log.warning(f"Réponse inattendue de l'IA: '{response.text.strip()}'. Approbation par sécurité.")
                return True, "Réponse IA non standard, approuvé par défaut."

        except Exception as e:
            self.log.error(f"Erreur lors de l'appel à l'API Gemini pour confirmation: {e}")
            return True, "Erreur API, approbation par défaut."

    def get_gemini_analysis(self, losing_trade_context: dict):
        """Demande à Gemini une analyse post-mortem sur un trade perdant."""
        if not self.model:
            return

        prompt = f"""
        Role: Tu es un analyste de performance quantitatif.
        Tâche: Analyse ce trade perdant sur XAUUSD pour identifier la cause la plus probable de l'échec.

        Contexte du Trade Perdant:
        - Direction prise : {losing_trade_context['direction']}
        - PnL final : {losing_trade_context['pnl']:.2f}
        
        Scores des stratégies au moment de l'ouverture (0-100):
        - Tendance (TREND) : {losing_trade_context.get('score_TREND', 0):.1f}
        - Structure (SMC) : {losing_trade_context.get('score_SMC', 0):.1f}
        - Contre-Tendance (MEAN_REV) : {losing_trade_context.get('score_MEAN_REV', 0):.1f}
        - Volatilité (VOL_BRK) : {losing_trade_context.get('score_VOL_BRK', 0):.1f}
        - Session Londres (LONDON_BRK) : {losing_trade_context.get('score_LONDON_BRK', 0):.1f}

        Analyse requise:
        1.  **Hypothèse Principale:** Fournis une hypothèse concise (15 mots max) sur la raison de l'échec. Ex: "Entrée en tendance tardive dans un marché en range." ou "Faux signal de cassure manquant de confirmation."
        2.  **Suggestion d'Action:** Suggère une action corrective sur les poids des stratégies pour le profil 'custom'. Ex: "Réduire le poids de VOL_BRK." ou "Augmenter la condition de confluence pour TREND."
        """
        
        self.log.info(f"Préparation d'une analyse IA post-mortem pour le trade perdant #{losing_trade_context['ticket']}.")
        
        try:
            response = self.model.generate_content(prompt)
            analysis_text = response.text.strip()
            # Utilise un log de niveau WARNING pour que l'analyse soit bien visible
            self.log.warning(f"ANALYSE IA (Trade #{losing_trade_context['ticket']}):\n{analysis_text}")

        except Exception as e:
            self.log.error(f"Erreur lors de l'appel à l'API Gemini pour analyse post-mortem: {e}")