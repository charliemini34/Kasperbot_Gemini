import logging
from typing import Dict, Tuple

class Aggregator:
    """
    Agrège les scores bruts des stratégies en un score de confiance final (0-100) et une direction.
    Version améliorée pour mieux pondérer la conviction du marché.
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
        Calcule le score final en additionnant les forces pondérées acheteuses et vendeuses.
        Retourne (score de 0 à 100, direction).
        """
        if not raw_scores:
            return 0.0, "NEUTRAL"

        buy_force = 0.0
        sell_force = 0.0

        for strategy, result in raw_scores.items():
            if not isinstance(result, dict) or 'score' not in result or 'direction' not in result:
                continue

            score = result.get('score', 0.0)
            direction = result.get('direction', 'NEUTRAL').upper()
            weight = self.weights.get(strategy)

            if weight is None:
                self.log.debug(f"Poids non trouvé pour la stratégie '{strategy}', elle sera ignorée.")
                continue

            # On ajoute la contribution pondérée de chaque stratégie à la force correspondante
            if direction == "BUY":
                buy_force += score * weight
            elif direction == "SELL":
                sell_force += score * weight
        
        # Le score final est la différence absolue, reflétant la dominance d'un camp
        if buy_force > sell_force:
            # Le score est la force du camp gagnant, plafonné à 100
            final_score = min(100.0, buy_force)
            final_direction = "BUY"
        elif sell_force > buy_force:
            final_score = min(100.0, sell_force)
            final_direction = "SELL"
        else:
            final_score = 0.0
            final_direction = "NEUTRAL"
            
        return final_score, final_direction