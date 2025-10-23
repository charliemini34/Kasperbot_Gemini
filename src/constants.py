# Fichier: src/constants.py
# Version: 1.1.0 (NEUTRAL-Fix)

# --- Noms des Patterns ---
PATTERN_ORDER_BLOCK = "ORDER_BLOCK"
PATTERN_CHOCH = "CHANGE_OF_CHARACTER"
PATTERN_INBALANCE = "INBALANCE" # Renommé pour correspondre à la demande
PATTERN_LIQUIDITY_GRAB = "LIQUIDITY_GRAB"
PATTERN_AMD = "SMC_AMD_SESSION" # Ajout du pattern AMD

# --- Directions de Trade ---
BUY = "BUY"
SELL = "SELL"
NEUTRAL = "NEUTRAL" # Ajouté pour Sugg 7 (Filtre Tendance)

# Constante pour les zones Premium/Discount (SMC)
# 0.5 représente l'équilibre (50%)
PREMIUM_THRESHOLD = 0.5