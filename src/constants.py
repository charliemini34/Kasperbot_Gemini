# Fichier: src/constants.py
# Version: 1.1.0 (Bias-Added) # <-- Version mise à jour

# --- Noms des Patterns ---
PATTERN_ORDER_BLOCK = "ORDER_BLOCK"
PATTERN_CHOCH = "CHANGE_OF_CHARACTER"
PATTERN_INBALANCE = "INBALANCE"
PATTERN_LIQUIDITY_GRAB = "LIQUIDITY_GRAB"
PATTERN_AMD = "SMC_AMD_SESSION"
# Ajouter BOS comme constante (même si pas dans config pour l'instant)
PATTERN_BOS = "BREAK_OF_STRUCTURE"

# --- Directions de Trade ---
BUY = "BUY"
SELL = "SELL"
# Ajouter biais neutre
ANY = "ANY"

# Constante pour les zones Premium/Discount (SMC)
PREMIUM_THRESHOLD = 0.5