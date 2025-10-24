# Fichier: src/constants.py
# Version: 1.2.0 (Sugg 4.2)

# --- Noms des Patterns ---
PATTERN_ORDER_BLOCK = "ORDER_BLOCK"
PATTERN_INBALANCE = "INBALANCE" # FVG (Fair Value Gap)
PATTERN_LIQUIDITY_GRAB = "LIQUIDITY_GRAB"
PATTERN_AMD = "SMC_AMD_SESSION"

# --- (Sugg 4.2) Terminologie Structurelle ---
PATTERN_BOS = "BREAK_OF_STRUCTURE" # Continuation (Anciennement CHOCH dans le code)
PATTERN_CHOCH = "CHANGE_OF_CHARACTER" # Renversement (Nouveau)

# --- Directions de Trade ---
BUY = "BUY"
SELL = "SELL"
NEUTRAL = "NEUTRAL"

# Constante pour les zones Premium/Discount (SMC)
PREMIUM_THRESHOLD = 0.5