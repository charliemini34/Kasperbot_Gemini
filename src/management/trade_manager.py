# __version__ = "1.5"
# Nom du fichier : src/management/trade_manager.py
import logging
from typing import List, Dict, Any, Optional

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# CONSTANTE pour le commentaire de gestion (pour identifier nos trades)
BOT_MAGIC_COMMENT = "KASPERBOT_SMC" 

def move_sl_to_break_even(trade, symbol_tick_info: dict, config: dict) -> Optional[Dict[str, Any]]:
    """
    NOUVELLE LOGIQUE (v1.5)
    Vérifie si le SL doit être déplacé à Break-Even (BE).
    Le BE est déclenché lorsque le trade atteint +1R (configurable).
    """
    try:
        be_trigger_rrr = config['trading'].get('be_trigger_rrr', 1.0)
        entry_price = trade.price_open
        sl_price = trade.sl
        current_sl = trade.sl
        direction = trade.type
        
        # Vérifier si le SL est valide et n'est pas déjà à BE
        if sl_price == 0.0 or sl_price == entry_price:
            return None # Pas de SL initial ou déjà à BE

        # Calculer 1R (la distance de risque initiale)
        initial_risk_dist = abs(entry_price - sl_price)
        if initial_risk_dist == 0:
            return None

        # Calculer le prix de déclenchement du BE
        trigger_price = None
        current_price = 0.0

        # trade.type == 0 (BUY), trade.type == 1 (SELL)
        if direction == 0: # BUY
            trigger_price = entry_price + (initial_risk_dist * be_trigger_rrr)
            current_price = symbol_tick_info.get('bid', 0)
            
            if current_price >= trigger_price and current_sl < entry_price:
                logger.info(f"TRADE {trade.ticket}: Déclenchement BREAK-EVEN (BUY). Prix {current_price:.5f} > Cible {trigger_price:.5f}")
                return {
                    "action": "MODIFY",
                    "trade_ticket": trade.ticket,
                    "new_sl": entry_price,
                    "new_tp": trade.tp # Garder le même TP
                }
                
        elif direction == 1: # SELL
            trigger_price = entry_price - (initial_risk_dist * be_trigger_rrr)
            current_price = symbol_tick_info.get('ask', 0)

            if current_price <= trigger_price and current_sl > entry_price:
                logger.info(f"TRADE {trade.ticket}: Déclenchement BREAK-EVEN (SELL). Prix {current_price:.5f} < Cible {trigger_price:.5f}")
                return {
                    "action": "MODIFY",
                    "trade_ticket": trade.ticket,
                    "new_sl": entry_price,
                    "new_tp": trade.tp # Garder le même TP
                }
                
    except Exception as e:
        logger.error(f"Erreur lors de la vérification Break-Even pour trade {trade.ticket}: {e}")
        
    return None

def apply_trailing_stop(trade, structure_ltf: dict, symbol_info: dict, config: dict) -> Optional[Dict[str, Any]]:
    """
    NOUVELLE LOGIQUE (v1.5)
    Applique un Trailing Stop (TS) structurel.
    Le SL suit le dernier pivot (SL pour un BUY, SH pour un SELL).
    """
    try:
        direction = trade.type
        current_sl = trade.sl
        sl_buffer_pips = config['trading'].get('sl_buffer_pips', 2.0)
        point = symbol_info.get('point', 0.00001)
        buffer_amount = sl_buffer_pips * point
        
        new_structural_sl = None

        if direction == 0: # BUY
            # Trailing : On suit le dernier Swing Low (SL)
            new_sl_pivot = structure_ltf.get('last_sl')
            if new_sl_pivot:
                # Placer le SL sous le dernier SL
                new_structural_sl = new_sl_pivot - buffer_amount
                # On ne déplace le SL que s'il est plus haut que l'actuel
                if new_structural_sl > current_sl:
                    logger.info(f"TRADE {trade.ticket}: TRAILING STOP (BUY). Nouveau SL structurel {new_structural_sl:.5f} (basé sur SL {new_sl_pivot:.5f})")
                    
        elif direction == 1: # SELL
            # Trailing : On suit le dernier Swing High (SH)
            new_sh_pivot = structure_ltf.get('last_sh')
            if new_sh_pivot:
                # Placer le SL au-dessus du dernier SH
                new_structural_sl = new_sh_pivot + buffer_amount
                # On ne déplace le SL que s'il est plus bas que l'actuel
                if new_structural_sl < current_sl:
                    logger.info(f"TRADE {trade.ticket}: TRAILING STOP (SELL). Nouveau SL structurel {new_structural_sl:.5f} (basé sur SH {new_sh_pivot:.5f})")

        if new_structural_sl and new_structural_sl != current_sl:
            return {
                "action": "MODIFY",
                "trade_ticket": trade.ticket,
                "new_sl": new_structural_sl,
                "new_tp": trade.tp # Garder le même TP
            }

    except Exception as e:
        logger.error(f"Erreur lors de l'application du Trailing Stop pour trade {trade.ticket}: {e}")
        
    return None

def take_partial_profit(trade, symbol_tick_info: dict, config: dict) -> Optional[Dict[str, Any]]:
    """
    (PLACEHOLDER v1.5)
    Logique pour prendre des profits partiels (TP Partiels).
    
    NOTE : Une implémentation robuste nécessite de stocker l'état du trade
    (ex: 'initial_risk_r', 'partials_taken') dans shared_state ou une base de données,
    car les informations du trade (comme le SL original) sont perdues après un BE ou un Trailing.
    """
    # Exemple de logique (non activé) :
    # if config['trading'].get('enable_partials', False):
    #     # 1. Récupérer l'état du trade (ex: R initial)
    #     # 2. Vérifier si le prix a atteint le TP1 (ex: 2R)
    #     # 3. Si oui, et si le partiel n'a pas été pris :
    #     #    return {
    #     #        "action": "CLOSE_PARTIAL",
    #     #        "trade_ticket": trade.ticket,
    #     #        "volume_to_close": trade.volume * 0.5 # Fermer 50%
    #     #    }
    pass # Non implémenté dans cette version
    return None

def manage_open_trades(
    open_positions: List[Any], 
    symbol_info: Dict[str, Any], 
    config: Dict[str, Any],
    structure_ltf: Dict[str, Any],  # NÉCESSAIRE pour Trailing SL
    symbol_tick_info: Dict[str, Any] # NÉCESSAIRE pour BE
) -> List[Dict[str, Any]]:
    """
    NOUVELLE LOGIQUE (v1.5)
    Orchestre la gestion des trades ouverts (BE, Trailing, Partiels).
    
    REMARQUE IMPORTANTE : La signature de cette fonction a été MISE À JOUR (v1.5)
    pour inclure 'structure_ltf' et 'symbol_tick_info'.
    L'orchestrateur (smc_orchestrator.py) doit être mis à jour pour passer ces arguments.
    """
    modification_requests = []
    
    if not open_positions:
        return modification_requests

    try:
        # Récupérer les statuts de gestion depuis la config
        enable_be = config['trading'].get('enable_break_even', True)
        enable_ts = config['trading'].get('enable_trailing_stop', True)
        enable_partials = config['trading'].get('enable_partials', False) # Désactivé par défaut
        
        # Filtrer les trades gérés par ce bot (basé sur le commentaire)
        bot_trades = [t for t in open_positions if t.comment == BOT_MAGIC_COMMENT and t.symbol == symbol_info['name']]
        
        for trade in bot_trades:
            # 1. Gérer le Break-Even (prioritaire)
            if enable_be:
                be_request = move_sl_to_break_even(trade, symbol_tick_info, config)
                if be_request:
                    modification_requests.append(be_request)
                    # Si on met à BE, on ne fait pas de Trailing Stop ce tick-ci
                    # pour éviter les requêtes conflictuelles.
                    continue 

            # 2. Gérer le Trailing Stop (si BE non déclenché)
            if enable_ts:
                ts_request = apply_trailing_stop(trade, structure_ltf, symbol_info, config)
                if ts_request:
                    modification_requests.append(ts_request)
                    continue

            # 3. Gérer les TP Partiels (si activé)
            if enable_partials:
                partial_request = take_partial_profit(trade, symbol_tick_info, config)
                if partial_request:
                    modification_requests.append(partial_request)
                    
    except KeyError as e:
        logger.error(f"Erreur de gestion : Clé de configuration manquante : {e}")
    except Exception as e:
        logger.error(f"Erreur inattendue lors de la gestion des trades : {e}")

    return modification_requests

if __name__ == "__main__":
    # MISE À JOUR (v1.5)
    # Bloc de test pour valider la gestion de trade
    
    logger.info("--- Début du Test de Gestion de Trade v1.5 ---")

    # Simuler un trade (type 'object' pour simuler la réponse de MT5)
    class MockTrade:
        def __init__(self, ticket, price_open, sl, tp, type, volume, comment, symbol):
            self.ticket = ticket
            self.price_open = price_open
            self.sl = sl
            self.tp = tp
            self.type = type # 0=BUY, 1=SELL
            self.volume = volume
            self.comment = comment
            self.symbol = symbol

    # 1. Configuration
    test_config = {
        "trading": {
            "be_trigger_rrr": 1.0,
            "enable_break_even": True,
            "enable_trailing_stop": True,
            "enable_partials": False,
            "sl_buffer_pips": 2.0
        }
    }
    test_symbol_info = {"name": "EURUSD", "point": 0.00001}
    
    # Scénario 1: Test du Break-Even (BUY)
    logger.info("Test Scénario 1 : Déclenchement Break-Even (BUY)")
    trade_be = MockTrade(1, 1.10000, 1.09900, 1.10500, 0, 0.1, BOT_MAGIC_COMMENT, "EURUSD")
    # Risk = 1.10000 - 1.09900 = 0.00100 (10 pips)
    # Cible BE = 1.10000 + 0.00100 = 1.10100
    tick_be = {"bid": 1.10105} # Le prix dépasse la cible BE
    structure_be = {} # Pas nécessaire pour le BE

    requests_be = manage_open_trades([trade_be], test_symbol_info, test_config, structure_be, tick_be)
    
    if requests_be and requests_be[0]['action'] == 'MODIFY' and requests_be[0]['new_sl'] == 1.10000:
        logger.info(f"Résultat Test 1 : SUCCÈS. Demande de BE générée : {requests_be[0]}")
    else:
        logger.error(f"Résultat Test 1 : ÉCHEC. {requests_be}")

    # Scénario 2: Test du Trailing Stop (SELL)
    logger.info("Test Scénario 2 : Déclenchement Trailing Stop (SELL)")
    trade_ts = MockTrade(2, 1.20000, 1.20100, 1.19500, 1, 0.1, BOT_MAGIC_COMMENT, "EURUSD")
    tick_ts = {"ask": 1.19900} # En profit, mais BE non atteint
    # Le SL doit suivre le dernier SH.
    structure_ts = {"last_sh": 1.19950, "last_sl": 1.19800} # Nouveau SH plus bas que le SL initial
    
    # SL initial = 1.20100
    # Nouveau SH = 1.19950
    # Nouveau SL = 1.19950 + 0.00020 (buffer) = 1.19970
    # 1.19970 < 1.20100 -> Le SL doit être déplacé

    requests_ts = manage_open_trades([trade_ts], test_symbol_info, test_config, structure_ts, tick_ts)
    
    if requests_ts and requests_ts[0]['action'] == 'MODIFY' and abs(requests_ts[0]['new_sl'] - 1.19970) < 0.00001:
        logger.info(f"Résultat Test 2 : SUCCÈS. Demande de TS générée : {requests_ts[0]}")
    else:
        logger.error(f"Résultat Test 2 : ÉCHEC. {requests_ts}")

    logger.info("--- Fin du Test de Gestion de Trade v1.5 ---")