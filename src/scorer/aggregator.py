import logging
from typing import Dict, Tuple

class Aggregator:
    """
    Agrège les scores bruts des stratégies en un score de confiance final (0-100) et une direction.
    Cette version a été corrigée pour assurer un calcul robuste et une échelle de score correcte.
    """
    def __init__(self, weights: Dict[str, float]):
        self.weights = weights if weights else {}
        self.log = logging.getLogger(self.__class__.__name__)
        
        # Normaliser les poids pour que leur somme soit égale à 1.0, pour une logique saine.
        total_weight = sum(self.weights.values())
        if total_weight > 0 and abs(total_weight - 1.0) > 1e-9:
            self.log.warning(f"La somme des poids n'est pas 1.0 (total={total_weight:.2f}). Normalisation en cours.")
            self.weights = {k: v / total_weight for k, v in self.weights.items()}

    def calculate_final_score(self, raw_scores: dict) -> Tuple[float, str]:
        """
        Calcule le score final en se basant sur la "lutte" entre les forces acheteuses et vendeuses.
        Retourne (score de 0 à 100, direction).
        """
        if not raw_scores:
            return 0.0, "NEUTRAL"

        buy_momentum = 0.0
        sell_momentum = 0.0
        
        total_buy_weight = 0.0
        total_sell_weight = 0.0

        for strategy, result in raw_scores.items():
            # S'assurer que le résultat est un dictionnaire avec score et direction
            if not isinstance(result, dict) or 'score' not in result or 'direction' not in result:
                continue

            score = result.get('score', 0.0)
            direction = result.get('direction', 'NEUTRAL').upper()
            weight = self.weights.get(strategy)

            # Si une stratégie n'a pas de poids défini, on l'ignore pour ne pas fausser le calcul.
            if weight is None:
                self.log.debug(f"Poids non trouvé pour la stratégie '{strategy}', elle sera ignorée.")
                continue

            if direction == "BUY":
                buy_momentum += score * weight
                total_buy_weight += weight
            elif direction == "SELL":
                sell_momentum += score * weight
                total_sell_weight += weight

        # Calculer la force moyenne pondérée pour chaque côté
        avg_buy_force = (buy_momentum / total_buy_weight) if total_buy_weight > 0 else 0.0
        avg_sell_force = (sell_momentum / total_sell_weight) if total_sell_weight > 0 else 0.0

        # Le score final est la différence absolue entre les deux forces.
        # Cela mesure la "dominance" d'un côté sur l'autre.
        if avg_buy_force > avg_sell_force:
            final_score = avg_buy_force - avg_sell_force
            final_direction = "BUY"
        elif avg_sell_force > avg_buy_force:
            final_score = avg_sell_force - avg_buy_force
            final_direction = "SELL"
        else:
            final_score = 0.0
            final_direction = "NEUTRAL"
            
        # Assurer que le score est bien entre 0 et 100
        final_score = max(0.0, min(100.0, final_score))

        return final_score, final_direction

