import logging
from typing import Dict, Tuple

class Aggregator:
    """
    Agrège les scores bruts des stratégies en un score de confiance final (0-100) et une direction.
    Cette version inclut une logique pour les signaux à "haute conviction" afin de 
    donner plus de poids aux configurations de marché exceptionnelles.
    """
    def __init__(self, weights: Dict[str, float]):
        self.weights = weights if weights else {}
        self.log = logging.getLogger(self.__class__.__name__)
        
        # Normalisation des poids pour que leur somme soit égale à 1.0
        total_weight = sum(self.weights.values())
        if total_weight > 0 and abs(total_weight - 1.0) > 1e-9:
            self.log.warning(f"La somme des poids n'est pas 1.0 (total={total_weight:.2f}). Normalisation en cours.")
            self.weights = {k: v / total_weight for k, v in self.weights.items()}

    def calculate_final_score(self, raw_scores: dict) -> Tuple[float, str]:
        """
        Calcule le score final en se basant sur une moyenne pondérée et des bonus de conviction.
        
        Retourne:
            Tuple[float, str]: (score final de 0 à 100, direction "BUY", "SELL" ou "NEUTRAL").
        """
        if not raw_scores:
            return 0.0, "NEUTRAL"

        buy_force = 0.0
        sell_force = 0.0
        
        high_conviction_buy_signals = 0
        high_conviction_sell_signals = 0
        super_conviction_buy = 0
        super_conviction_sell = 0

        for strategy, result in raw_scores.items():
            if not isinstance(result, dict) or 'score' not in result or 'direction' not in result:
                continue

            score = result.get('score', 0.0)
            direction = result.get('direction', 'NEUTRAL').upper()
            weight = self.weights.get(strategy, 0) # Ignorer les stratégies sans poids

            if weight == 0:
                self.log.debug(f"Poids non trouvé ou nul pour la stratégie '{strategy}', elle sera ignorée dans le calcul pondéré.")
                continue

            # 1. Calcul des forces pondérées (logique de base)
            if direction == "BUY":
                buy_force += score * weight
            elif direction == "SELL":
                sell_force += score * weight
            
            # 2. Détection des signaux pour les bonus de conviction
            if score > 90:
                if direction == "BUY": super_conviction_buy = max(super_conviction_buy, score)
                elif direction == "SELL": super_conviction_sell = max(super_conviction_sell, score)
            if score > 70:
                if direction == "BUY": high_conviction_buy_signals += 1
                elif direction == "SELL": high_conviction_sell_signals += 1
        
        # 3. Calcul du score final basé sur la force dominante
        final_score = 0.0
        final_direction = "NEUTRAL"

        if buy_force > sell_force:
            final_score = buy_force
            final_direction = "BUY"
        elif sell_force > buy_force:
            final_score = sell_force
            final_direction = "SELL"
        
        # 4. Application des bonus de conviction
        conviction_score = 0.0
        if super_conviction_buy > 0 and final_direction == "BUY":
            # Si un signal est > 90, on donne un score de base élevé + le score du signal
            conviction_score = 75 + (super_conviction_buy * 0.25)
            self.log.info(f"Bonus de conviction 'Super' détecté pour BUY (score > 90).")
        elif high_conviction_buy_signals >= 2 and final_direction == "BUY":
            # Si au moins 2 signaux sont > 70, score de base + moyenne pondérée
            conviction_score = 65 + (buy_force * 0.35)
            self.log.info(f"Bonus de conviction 'Confluence' détecté pour BUY (2+ scores > 70).")
        
        if super_conviction_sell > 0 and final_direction == "SELL":
            conviction_score = 75 + (super_conviction_sell * 0.25)
            self.log.info(f"Bonus de conviction 'Super' détecté pour SELL (score > 90).")
        elif high_conviction_sell_signals >= 2 and final_direction == "SELL":
            conviction_score = 65 + (sell_force * 0.35)
            self.log.info(f"Bonus de conviction 'Confluence' détecté pour SELL (2+ scores > 70).")

        # Le score final est le maximum entre le calcul pondéré et le score de conviction
        final_score = max(final_score, conviction_score)

        return min(100.0, final_score), final_direction