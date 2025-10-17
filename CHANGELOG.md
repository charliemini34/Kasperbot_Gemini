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
    
    # Changelog - KasperBot

## v14.0.0 - (17/10/2025) - Build "Guardian+ Enhanced"

### 🛡️ Sécurité et Stabilité (Priorité 1)

* **Gestion des Erreurs (`main.py`, `risk_manager.py`, `mt5_connector.py`)**:
    * Implémentation de blocs `try-except` plus granulaires dans toute l'application pour capturer des erreurs spécifiques (connexion, configuration, calcul) et éviter les arrêts critiques.
* **Money Management (`risk_manager.py`)**:
    * Ajout de garde-fous (`sanity checks`) dans `_calculate_volume` pour prévenir les divisions par zéro et les calculs basés sur des données invalides (ex: distance SL nulle).
    * Le trade est désormais annulé si le volume calculé est inférieur au minimum autorisé, protégeant ainsi le capital contre des ajustements de SL extrêmes.
* **Connexion MT5 (`mt5_connector.py`)**:
    * La fonction `connect` implémente maintenant une boucle de tentatives avec une attente exponentielle (`exponential backoff`) pour gérer les pertes de connexion temporaires de manière plus résiliente.
* **Configuration (`config.yaml`)**:
    * Le mode `live_trading_enabled` est maintenant défini sur `false` par défaut pour prévenir tout trading en réel non intentionnel.

### ✨ Fiabilité et Maintenabilité (Priorité 2)

* **Journalisation (Tous les modules)**:
    * La journalisation a été enrichie dans tous les modules critiques pour fournir des informations détaillées sur la logique de décision, le calcul des risques, la détection des patterns et l'état de la connexion.
    * Les logs d'erreurs incluent maintenant des `exc_info=True` pour tracer la pile d'appels complète, facilitant le débogage.
* **Validation des Stratégies SMC (`pattern_detector.py`)**:
    * **CORRECTION CRITIQUE**: La logique de détection pour `_detect_choch` (Change of Character) et `_detect_order_block` a été entièrement réécrite pour s'aligner sur les définitions SMC standards, ce qui augmente considérablement la précision des signaux. Un OB nécessite maintenant une rupture de structure (BOS) pour être considéré comme valide.
* **Structure du Code (`main.py`)**:
    * La boucle de trading principale a été restructurée pour suivre un flux logique plus clair (Connexion -> Config -> Positions -> Disjoncteur -> Analyse).

---

## v14.0.2 - (17/10/2025) - Build "Guardian+ Symbol Validation"

### 🐛 Corrections (Fixes)

* **`main.py`**:
    * **CORRECTION CRITIQUE**: Ajout d'une fonction `validate_symbols` qui vérifie au démarrage et après chaque rechargement de configuration si les symboles listés dans `config.yaml` sont bien disponibles sur la plateforme MetaTrader 5.
    * Le bot n'essaiera plus de créer une instance de `RiskManager` pour un symbole invalide, ce qui empêche le crash `ValueError: Informations de compte ou de symbole MT5 manquantes`.
    * Un message d'erreur clair est maintenant journalisé pour chaque symbole invalide, informant l'utilisateur de le corriger ou de le retirer de sa configuration.