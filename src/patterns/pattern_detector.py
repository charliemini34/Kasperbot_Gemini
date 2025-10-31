# Fichier: src/patterns/pattern_detector.py
# Version: 20.0.0 (SMC Brain Transplant)
# Description: Remplacement de la logique de détection par le nouvel orchestrateur SMC.
#              Conserve la structure de classe pour la compatibilité v19.

import logging
import pandas as pd
from typing import Dict, Any, Optional

# Importation du nouveau "cerveau" SMC
from src.strategy import smc_orchestrator
from src.constants import BUY, SELL

class PatternDetector:
    """
    Détecte les signaux de trading.
    Si la stratégie SMC est activée, il délègue l'analyse à l'orchestrateur SMC.
    Sinon, il utilise l'ancienne logique (désormais désactivée/simplifiée).
    """

    def __init__(self, config: dict):
        self.log = logging.getLogger(self.__class__.__name__)
        self.config = config
        self
        # Initialiser les informations sur les patterns détectés (pour l'API)
        self.detected_patterns_info = {}
        self.log.info("PatternDetector (v20.0.0 SMC) initialisé.")

    def get_detected_patterns_info(self) -> Dict[str, Any]:
        """Retourne les informations sur les derniers patterns détectés pour l'API."""
        return self.detected_patterns_info

    def detect_patterns(self, mtf_data: Dict[str, pd.DataFrame], connector: Any, symbol: str) -> Optional[Dict[str, Any]]:
        """
        Méthode principale pour la détection de pattern.
        Accepte maintenant mtf_data au lieu de ohlc_data.
        """
        # Réinitialiser les infos pour ce cycle
        self.detected_patterns_info = {}

        # --- NOUVELLE LOGIQUE SMC ---
        smc_config = self.config.get('smc_strategy', {})
        if smc_config.get('enabled', False):
            try:
                # Déléguer toute l'analyse au nouveau cerveau
                trade_signal = smc_orchestrator.find_smc_signal(mtf_data, self.config)
                
                if trade_signal:
                    # Mettre à jour l'info pour l'API
                    self.detected_patterns_info[trade_signal['pattern']] = {
                        "status": f"SIGNAL {trade_signal['direction']}",
                        "details": trade_signal['reason']
                    }
                    return trade_signal
                else:
                    # Mettre à jour l'info (aucun signal)
                    self.detected_patterns_info["SMC_OTE"] = {"status": "En attente OTE..."}
                    return None

            except Exception as e:
                self.log.error(f"Erreur critique dans l'orchestrateur SMC pour {symbol}: {e}", exc_info=True)
                self.detected_patterns_info["SMC_ERROR"] = {"status": "ERREUR", "details": str(e)}
                return None
        
        # --- ANCIENNE LOGIQUE (FALLBACK) ---
        else:
            self.log.warning(f"La stratégie SMC est désactivée pour {symbol}. Aucune autre logique de pattern n'est configurée.")
            self.detected_patterns_info["SMC_DISABLED"] = {"status": "Désactivé"}
            # (Ici se trouvait votre ancienne logique de détection v19)
            return None

# --- FONCTIONS DE BASE REQUISES PAR L'ORCHESTRATEUR ---
# Ces fonctions sont appelées par 'smc_orchestrator.py'
# Elles sont placées ici pour garder la logique de "pattern" au même endroit.

def find_imbalances(data: pd.DataFrame, config: dict) -> list:
    """
    Identifie les Imbalances (Fair Value Gaps - FVG) dans les données.
    """
    fvgs = []
    if len(data) < 3:
        return fvgs

    highs = data['high'].values
    lows = data['low'].values
    times = data.index

    for i in range(len(data) - 2):
        candle_1_high = highs[i]
        candle_1_low = lows[i]
        candle_3_high = highs[i+2]
        candle_3_low = lows[i+2]

        fvg_info = None

        # Bullish Imbalance (FVG Haussier)
        if candle_1_high < candle_3_low:
            fvg_info = {
                'type': BUY, 'top': candle_3_low, 'bottom': candle_1_high,
                'start_time': times[i], 'end_time': times[i+2]
            }
            
        # Bearish Imbalance (FVG Baissier)
        elif candle_1_low > candle_3_high:
            fvg_info = {
                'type': SELL, 'top': candle_1_low, 'bottom': candle_3_high,
                'start_time': times[i], 'end_time': times[i+2]
            }

        if fvg_info:
            mitigated_at = None
            for j in range(i + 3, len(data)):
                if fvg_info['type'] == BUY and lows[j] <= fvg_info['top']:
                    mitigated_at = times[j]
                    break 
                elif fvg_info['type'] == SELL and highs[j] >= fvg_info['bottom']:
                    mitigated_at = times[j]
                    break
            
            fvg_info['mitigated_at'] = mitigated_at
            fvgs.append(fvg_info)
            
    return fvgs

def find_order_blocks(data: pd.DataFrame, config: dict) -> list:
    """
    Identifie les Order Blocks (OB) potentiels.
    """
    obs = []
    if len(data) < 2:
        return obs
    
    opens = data['open'].values
    closes = data['close'].values
    highs = data['high'].values
    lows = data['low'].values
    times = data.index

    for i in range(1, len(data)):
        prev_open, prev_close = opens[i-1], closes[i-1]
        prev_high, prev_low = highs[i-1], lows[i-1]
        curr_open, curr_close = opens[i], closes[i]
        curr_low, curr_high = lows[i], highs[i]

        # Bullish OB (Haussier): Bougie baissière suivie d'une impulsion haussière
        if (prev_close < prev_open and      # Bougie 1 baissière
            curr_close > curr_open and      # Bougie 2 haussière
            curr_close > prev_high):        # Impulsion
            
            obs.append({
                'type': BUY, 'top': prev_high, 'bottom': prev_low, 'time': times[i-1]
            })

        # Bearish OB (Baissier): Bougie haussière suivie d'une impulsion baissière
        elif (prev_close > prev_open and    # Bougie 1 haussière
              curr_close < curr_open and    # Bougie 2 baissière
              curr_close < prev_low):       # Impulsion
            
            obs.append({
                'type': SELL, 'top': prev_high, 'bottom': prev_low, 'time': times[i-1]
            })

    return obs