import logging
import os
import google.generativeai as genai
from typing import Tuple

class AIAssistant:
    def __init__(self):
        self.log = logging.getLogger(self.__class__.__name__)
        self.api_key = os.getenv("GEMINI_API_KEY")
        self.model = None
        if self.api_key:
            try:
                genai.configure(api_key=self.api_key)
                self.model = genai.GenerativeModel('gemini-pro')
                self.log.info("Assistant IA initialisé avec succès (Gemini-Pro).")
            except Exception as e:
                self.log.error(f"Échec de l'initialisation de l'IA Gemini: {e}. L'assistant IA sera désactivé.")
                self.model = None
        else:
            self.log.warning("Variable d'environnement GEMINI_API_KEY non trouvée. L'assistant IA sera désactivé.")

    def confirm_trade_with_ai(self, trade_signal: dict) -> Tuple[bool, str]:
        """Demande à Gemini un second avis sur une opportunité de trade."""
        if not self.model:
            return True, "IA non configurée, approbation par défaut."

        prompt = f"""
        Analyse de confirmation de trade sur XAUUSD. Mon système a détecté une opportunité.
        Analyse ce signal et réponds uniquement par "APPROUVÉ" ou "REJETÉ", suivi d'une justification très courte (moins de 15 mots).
        Sois critique : si un score de contre-tendance est élevé alors que le signal est de tendance, c'est un drapeau rouge.

        Détails du Signal :
        - Direction proposée : {trade_signal['direction']}
        - Confiance du système : {trade_signal['confidence']:.1f}/100
        
        Scores des stratégies contributives :
        - Tendance (TREND) : {trade_signal['scores'].get('TREND', {}).get('score', 0):.1f}
        - Structure de Marché (SMC) : {trade_signal['scores'].get('SMC', {}).get('score', 0):.1f}
        - Contre-Tendance (MEAN_REV) : {trade_signal['scores'].get('MEAN_REV', {}).get('score', 0):.1f}
        - Cassure de Volatilité (VOL_BRK) : {trade_signal['scores'].get('VOL_BRK', {}).get('score', 0):.1f}

        Ton avis concis :
        """
        
        try:
            response = self.model.generate_content(prompt)
            ai_response_text = response.text.strip()
            
            if "APPROUVÉ" in ai_response_text.upper():
                return True, ai_response_text
            else:
                return False, ai_response_text

        except Exception as e:
            self.log.error(f"Erreur lors de l'appel à l'API Gemini pour confirmation: {e}")
            return True, "Erreur API, approbation par défaut."

    def get_gemini_analysis(self, losing_trade_context: dict):
        """Demande à Gemini une analyse post-mortem sur un trade perdant."""
        if not self.model:
            return

        prompt = f"""
        Analyse post-mortem d'un trade perdant sur XAUUSD.
        Objectif : comprendre pourquoi le trade a probablement échoué et suggérer une correction.

        Contexte du Trade Perdant :
        - Direction : {losing_trade_context['direction']}
        - PnL : {losing_trade_context['pnl']:.2f}
        
        Scores des stratégies au moment de l'ouverture (0-100) :
        - Tendance (TREND) : {losing_trade_context.get('score_TREND', 0):.1f}
        - Structure (SMC) : {losing_trade_context.get('score_SMC', 0):.1f}
        - Contre-Tendance (MEAN_REV) : {losing_trade_context.get('score_MEAN_REV', 0):.1f}
        - Volatilité (VOL_BRK) : {losing_trade_context.get('score_VOL_BRK', 0):.1f}
        - Session Londres (LONDON_BRK) : {losing_trade_context.get('score_LONDON_BRK', 0):.1f}

        1. Fournis une hypothèse principale concise sur la raison de l'échec (ex: "Signal de tendance dans un marché en range", "Chasse à la liquidité contre la tendance principale").
        2. Suggère une action corrective sur les poids des stratégies (ex: "Réduire le poids de VOL_BRK", "Augmenter la condition de confluence pour TREND").
        """
        
        self.log.info(f"Préparation d'une analyse IA pour le trade perdant #{losing_trade_context['ticket']}.")
        
        try:
            response = self.model.generate_content(prompt)
            analysis_text = response.text.strip()
            self.log.warning(f"ANALYSE IA (Trade #{losing_trade_context['ticket']}):\n{analysis_text}")

        except Exception as e:
            self.log.error(f"Erreur lors de l'appel à l'API Gemini pour analyse post-mortem: {e}")