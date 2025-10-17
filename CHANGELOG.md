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
    
    # Changelog - KasperBot

## v10.0.0 - (17/10/2025) - Build "Guardian"

### 🛡️ Sécurité et Robustesse (Security & Robustness)

* **RiskManager (`src/risk/risk_manager.py`)**:
    * **NOUVEAU**: Implémentation d'un **disjoncteur (circuit breaker)** qui arrête toute nouvelle activité de trading si la limite de perte journalière (configurable dans `config.yaml`) est atteinte. C'est une sécurité essentielle contre les conditions de marché extrêmes.
* **Main (`main.py`)**:
    * La boucle principale intègre désormais la vérification du disjoncteur avant chaque cycle d'analyse, passant le bot en mode d'urgence si la limite de perte est touchée.
    * La gestion des exceptions a été affinée pour différencier les erreurs de connexion MT5 des autres erreurs critiques, permettant des stratégies de reprise plus intelligentes.
* **MT5Executor (`src/execution/mt5_executor.py`)**:
    * La logique d'archivage des trades a été renforcée pour gérer les cas où l'historique des transactions de MT5 n'est pas immédiatement disponible, évitant ainsi la perte de données de performance.
    * La création du fichier `trade_history.csv` est maintenant gérée de manière plus robuste.

### ✨ Améliorations Stratégiques (Strategy Enhancements)

* **PatternDetector (`src/patterns/pattern_detector.py`)**:
    * **CORRECTION CRITIQUE**: La logique de détection des **Order Blocks (OB)** a été entièrement réécrite pour se conformer aux définitions standards du SMC. Un signal n'est maintenant considéré comme valide que s'il est suivi d'une claire **rupture de structure (BOS)**, ce qui augmente considérablement la pertinence et la qualité des signaux.
* **Backtester (`src/backtest/backtester.py`)**:
    * Le moteur de simulation a été mis à jour pour utiliser la nouvelle logique de détection de patterns et les nouvelles règles de gestion des risques, garantissant que les backtests reflètent fidèlement la performance de la version actuelle du bot.