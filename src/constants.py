# Fichier: src/constants.py
# Version: 2.0
#
# Définit les constantes globales utilisées à travers l'application.

import logging

# Niveaux de logging
LOG_LEVEL = logging.INFO
MAX_LOG_ENTRIES = 200

# --- Noms des Patterns SMC ---
PATTERN_ORDER_BLOCK = "ORDER_BLOCK"
PATTERN_INBALANCE = "INBALANCE" # FVG (Fair Value Gap)
PATTERN_LIQUIDITY_GRAB = "LIQUIDITY_GRAB"
PATTERN_AMD = "SMC_AMD_SESSION"

# --- Terminologie Structurelle ---
PATTERN_BOS = "BREAK_OF_STRUCTURE" # Continuation
PATTERN_CHOCH = "CHANGE_OF_CHARACTER" # Renversement

# --- Directions de Trade (Analyse) ---
BUY = "BUY"
SELL = "SELL"
NEUTRAL = "NEUTRAL"

# Constante pour les zones Premium/Discount (SMC)
PREMIUM_THRESHOLD = 0.5

# --- Types d'Ordres MT5 (Exécution) ---
# Nous importons MetaTrader5 ici pour obtenir les valeurs numériques officielles
# et les stocker dans nos constantes.
try:
    import MetaTrader5 as mt5
    
    # Types d'ordres au marché (Market Orders)
    ORDER_TYPE_BUY = mt5.ORDER_TYPE_BUY       # Valeur = 0
    ORDER_TYPE_SELL = mt5.ORDER_TYPE_SELL     # Valeur = 1
    
    # Types d'ordres en attente (Pending Orders)
    ORDER_TYPE_BUY_LIMIT = mt5.ORDER_TYPE_BUY_LIMIT     # Valeur = 2
    ORDER_TYPE_SELL_LIMIT = mt5.ORDER_TYPE_SELL_LIMIT   # Valeur = 3
    ORDER_TYPE_BUY_STOP = mt5.ORDER_TYPE_BUY_STOP       # Valeur = 4
    ORDER_TYPE_SELL_STOP = mt5.ORDER_TYPE_SELL_STOP     # Valeur = 5

except ImportError:
    # Fallback au cas où MetaTrader5 n'est pas installé (ex: tests unitaires)
    print("Avertissement: MetaTrader5 non trouvé. Utilisation de valeurs fallback pour les constantes d'ordre.")
    ORDER_TYPE_BUY = 0
    ORDER_TYPE_SELL = 1
    ORDER_TYPE_BUY_LIMIT = 2
    ORDER_TYPE_SELL_LIMIT = 3
    ORDER_TYPE_BUY_STOP = 4
    ORDER_TYPE_SELL_STOP = 5