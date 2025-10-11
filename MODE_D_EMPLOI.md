# Guide d'Utilisation - Bot de Trading XAUUSD Pro

Bonjour ! Ce guide est conçu pour vous aider à installer et à utiliser votre nouveau bot de trading, même si vous n'avez aucune connaissance en programmation.

## Étape 1 : Prérequis

1.  **Python** : Assurez-vous que Python est installé sur votre ordinateur. Si ce n'est pas le cas, téléchargez-le depuis `python.org` (prenez la version la plus récente).
2.  **MetaTrader 5** : Vous devez avoir le terminal MetaTrader 5 installé. Vous pouvez le télécharger depuis le site de votre courtier.
3.  **Compte de Trading** : Pour commencer, utilisez impérativement un **compte de démonstration (démo)** pour tester le bot sans risquer d'argent réel.



## Étape 2 : Installation des Outils

1.  **Ouvrez un terminal** :
    * **Windows** : Cherchez "Command Prompt" ou "PowerShell" dans le menu Démarrer.
    * **Mac** : Cherchez "Terminal" dans vos applications.
2.  **Naviguez vers votre dossier** : Dans le terminal, tapez `cd ` suivi d'un espace, puis glissez-déposez votre dossier `Bot_Trading_XAUUSD` dans la fenêtre du terminal. Appuyez sur Entrée. Le chemin devrait apparaître automatiquement.
3.  **Installez les dépendances** : Tapez la commande suivante et appuyez sur Entrée. Cela installera automatiquement tous les outils nécessaires au bot.
    ```
    pip install -r requirements.txt
    ```
    Si tout se passe bien, vous verrez des barres de téléchargement et d'installation.

## Étape 3 : Configuration du Bot

1.  **Ouvrez `config.yaml`** avec un éditeur de texte simple (Bloc-notes, TextEdit, VSCode...).
2.  **Renseignez vos identifiants MetaTrader 5** :
    * `login`: Votre numéro de compte MT5.
    * `password`: Le mot de passe de votre compte MT5.
    * `server`: Le nom du serveur de votre courtier (ex: "MetaQuotes-Demo").
3.  **Configurez le risque (TRÈS IMPORTANT)** :
    * `live_trading_enabled`: **Laissez sur `false`** pour commencer en mode simulation (sans risque). Vous passerez à `true` uniquement lorsque vous serez absolument certain de vouloir trader en réel.
    * `risk_per_trade`: Le pourcentage de votre capital que vous êtes prêt à risquer par trade (ex: `0.01` pour 1%). C'est une sécurité essentielle.
    * **Sécurité des profits** : Vous pouvez activer/désactiver le "break-even" (mise à l'équilibre) et le "trailing stop" (stop suiveur) pour sécuriser vos gains automatiquement.
4.  **Enregistrez et fermez le fichier** `config.yaml`.

## Étape 4 : Lancement du Bot

1.  **Lancez MetaTrader 5** et connectez-vous à votre compte.
2.  **Activez le trading algorithmique** : Dans MT5, cliquez sur le bouton qui ressemble à un chapeau de diplômé, intitulé **"Algo Trading"**. Il doit être vert.
3.  **Retournez au terminal** (celui de l'étape 2).
4.  **Lancez le bot** en tapant la commande suivante et en appuyant sur Entrée :
    ```
    python main.py
    ```
    Le terminal affichera des messages indiquant que le bot démarre et se connecte. Ne fermez pas cette fenêtre !

## Étape 5 : Utilisation de l'Interface de Contrôle

1.  **Ouvrez votre navigateur web** (Chrome, Firefox, etc.).
2.  **Accédez à l'adresse** : `http://127.0.0.1:5000`
3.  Vous verrez le tableau de bord du bot !

    

    * **État du Bot** : Indique si le bot est connecté, son mode (Live ou Simulation), et le profit/perte de la journée. Vous y trouverez aussi un **BOUTON D'ARRÊT D'URGENCE (KILL SWITCH)** pour tout couper instantanément.
    * **Positions Ouvertes** : Affiche en temps réel tous les trades en cours.
    * **Analyse des Stratégies** : Montre les scores de confiance pour chaque stratégie. C'est ici que vous voyez comment le bot "réfléchit".
    * **Journal d'Événements** : Affiche tous les messages importants : détections, ordres envoyés, erreurs, etc.

C'est tout ! Laissez le bot tourner avec le terminal et l'interface web ouverts. Commencez toujours par le mode simulation (`live_trading_enabled: false`) pendant plusieurs jours ou semaines pour vous assurer que tout fonctionne comme vous le souhaitez.



Guide d'Utilisation - Bot de Trading XAUUSD v2.0
Cette nouvelle version transforme votre bot en une véritable plateforme de contrôle et d'analyse.

Installation (Si vous mettez à jour)
Arrêtez l'ancien bot si il est en cours d'exécution (fermez la fenêtre du terminal).

Remplacez tous les anciens fichiers par les nouveaux que je vous ai fournis.

Mettez à jour les dépendances. Ouvrez un terminal dans le dossier du bot et lancez :

pip install -r requirements.txt

Les Nouvelles Fonctionnalités
1. Panneau de Configuration Dynamique
Vous n'avez plus besoin de modifier le fichier config.yaml à la main !

Comment ça marche ? Dans l'interface web, un nouveau panneau "Configuration" est apparu. Vous pouvez y ajuster en temps réel :

Le seuil de déclenchement des trades.

Les poids de chaque stratégie (pour donner plus ou moins d'importance à un "spécialiste").

Les paramètres de gestion du risque (Stop Loss, Take Profit, etc.).

Pour sauvegarder : Cliquez simplement sur le bouton "Sauvegarder la Configuration". Les changements sont appliqués immédiatement et enregistrés pour les prochains redémarrages.

[Image d'un panneau de configuration dans une interface web]

2. Journal en Temps Réel
Fini le besoin de regarder la fenêtre noire du terminal !

Comment ça marche ? Le panneau "Journal d'Événements" de l'interface affiche maintenant exactement les mêmes messages que le terminal, en temps réel. Vous pouvez tout suivre depuis votre navigateur.

3. Panneau de Backtesting
Testez vos idées de configuration sur des données historiques avant de risquer de l'argent.

Comment ça marche ?

Allez dans le nouveau panneau "Backtesting" sur l'interface.

Choisissez une période de test (dates de début et de fin).

Entrez un capital initial de simulation.

Cliquez sur "Lancer le Backtest".

Résultats : Le bot va simuler des mois ou des années de trading en quelques secondes. Une fois terminé, il affichera un rapport complet :

Profit/Perte final.

Drawdown maximum (la plus grosse perte subie depuis un pic).

Taux de réussite.

Nombre de trades effectués.

Un graphique de l'évolution de votre capital pour voir visuellement la performance.

[Image d'un rapport de backtest avec graphique de performance]

4. La Boucle d'Amélioration (Apprentissage)
C'est la fonctionnalité la plus avancée, elle fonctionne en arrière-plan.

Comment ça marche ?

Journalisation : Chaque trade que le bot effectue est désormais enregistré dans un nouveau fichier, trade_history.csv, avec tout le contexte (scores des stratégies, etc.).

Analyse : Périodiquement, le module performance_analyzer lit cet historique. Il calcule la performance réelle de chaque stratégie.

Suggestion d'Optimisation : Pour l'instant, le bot ne modifie pas ses propres poids automatiquement (c'est plus sûr). À la place, il affichera des suggestions dans le journal, du type : "ANALYSE: La stratégie SMC est très performante (80% de réussite). Envisagez d'augmenter son poids."

Préparation pour l'IA (Gemini) : La structure est en place pour qu'à l'avenir, on puisse envoyer les données d'un trade perdant à une IA (comme Gemini) en lui demandant : "Analyse ce trade et dis-moi pourquoi il a probablement échoué."

Avec ces outils, vous avez un contrôle total sur la stratégie et la performance de votre bot. Utilisez massivement le backtester pour trouver les réglages qui vous semblent les plus prometteurs avant de les appliquer en simulation.