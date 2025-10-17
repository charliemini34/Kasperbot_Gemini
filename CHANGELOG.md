# Changelog - KasperBot

## v9.5.0 - (17/10/2025) - Build "Robustesse"

### ✨ Améliorations (Features)

* **Backtester (`src/backtest/backtester.py`)**:
    * Le module a été entièrement réécrit pour simuler avec une haute-fidélité la stratégie de trading réelle.
    * Intégration d'un `MockConnector` pour servir les données historiques de manière contrôlée, y compris pour le filtre de tendance multi-temporelles, éliminant ainsi le *lookahead bias*.
    * La logique de la boucle de simulation réplique désormais exactement celle de `main.py`.
* **PatternDetector (`src/patterns/pattern_detector.py`)**:
    * La logique de détection des **Order Blocks** a été renforcée. Un signal n'est maintenant valide que s'il est suivi d'une claire **rupture de structure (BOS)**, ce qui augmente considérablement la qualité des signaux.

### 🐛 Corrections (Fixes)

* **Main (`main.py`)**:
    * La gestion des exceptions a été affinée. Les erreurs de connexion MT5 et les erreurs de données sont maintenant capturées et gérées spécifiquement pour éviter un arrêt complet du bot pour des problèmes potentiellement temporaires.