# Fichier: src/api/server.py
# Version: 3.0.1 (Correctif)
#
# Module pour démarrer un serveur web Flask qui sert de dashboard
# pour le monitoring du bot.
#
# V3.0.1: Ajout de l'initialisation du 'logger' (correction de NameError)
# V3.0: - Ajout d'un nouvel endpoint API /api/dashboard/state
#       - Mise à jour du HTML/CSS/JS pour afficher le statut des symboles
#         en temps réel et les signaux notés.
# --------------------------------------------------------------------------

import logging
from flask import Flask, jsonify, render_template_string
import threading

# --- DÉBUT CORRECTION V3.0.1 ---
# Initialisation du logger pour CE module
logger = logging.getLogger(__name__)
# --- FIN CORRECTION V3.0.1 ---

# Importations depuis notre module shared_state
try:
    # V3.0: Importation des nouvelles fonctions
    from src.shared_state import get_full_state, get_trade_log
except ImportError:
    logger.error("[API Server] Échec de l'importation depuis src.shared_state. Assurez-vous que le fichier existe.")
    # Fallback pour les anciennes versions (si nécessaire)
    from src.shared_state import get_trade_log
    
    # Définir une fonction factice pour éviter un crash si V3.0 n'est pas complet
    def get_full_state():
        return {}

__version__ = "3.0.1"

# Initialisation de l'application Flask
app = Flask(__name__)

# Désactiver les logs de requêtes Flask pour garder la console propre
# (Ceci est un logger différent, spécifique à 'werkzeug')
log = logging.getLogger('werkzeug')
log.setLevel(logging.WARNING)

# --- Contenu HTML/CSS/JS du Dashboard (V3.0) ---

HTML_CONTENT = """
<!DOCTYPE html>
<html lang="fr">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Kasperbot Dashboard</title>
    <style>
        body {
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
            background-color: #121212;
            color: #E0E0E0;
            margin: 0;
            padding: 20px;
        }
        .container {
            max-width: 1400px;
            margin: auto;
        }
        h1, h2 {
            color: #4CAF50; /* Vert Kasper */
            border-bottom: 2px solid #333;
            padding-bottom: 10px;
        }
        
        /* --- NOUVEAUX STYLES V3.0 --- */
        
        /* Boîte pour le signal actif */
        #active-signal-container {
            margin-bottom: 25px;
        }
        .signal-box {
            background-color: #1E1E1E;
            border: 1px solid #4CAF50; /* Vert */
            border-left-width: 5px;
            padding: 15px;
            border-radius: 8px;
            box-shadow: 0 4px 12px rgba(0, 0, 0, 0.4);
        }
        .signal-box-pending {
            background-color: #1E1E1E;
            border: 1px solid #555;
            padding: 15px;
            border-radius: 8px;
            color: #888;
            font-style: italic;
        }
        .signal-box h3 {
            margin-top: 0;
            color: #FFFFFF;
        }
        .signal-box pre {
            background-color: #252525;
            padding: 10px;
            border-radius: 4px;
            color: #FBC02D; /* Jaune/Or */
            font-family: "Courier New", Courier, monospace;
            font-size: 1.1em;
            font-weight: bold;
            white-space: pre-wrap; /* Retour à la ligne */
            cursor: pointer;
        }
        
        /* Liste de surveillance des symboles */
        #symbol-checklist-container {
            display: grid;
            grid-template-columns: repeat(auto-fill, minmax(400px, 1fr));
            gap: 15px;
            margin-bottom: 25px;
        }
        .symbol-row {
            background-color: #1E1E1E;
            padding: 12px;
            border-radius: 5px;
            border: 1px solid #333;
            font-size: 0.9em;
        }
        .symbol-row strong {
            color: #4CAF50;
            font-size: 1.1em;
        }
        .check-item {
            display: inline-block;
            padding: 3px 6px;
            border-radius: 4px;
            margin: 2px;
            font-size: 0.85em;
            font-weight: 500;
        }
        /* Statuts des checks */
        .check-pending {
            background-color: #424242; /* Gris */
            color: #BDBDBD;
        }
        .check-valid {
            background-color: #2E7D32; /* Vert foncé */
            color: #C8E6C9; /* Vert clair */
        }
        .check-invalid {
            background-color: #C62828; /* Rouge foncé */
            color: #FFCDD2; /* Rouge clair */
        }
        
        /* --- FIN NOUVEAUX STYLES V3.0 --- */

        /* Tableaux (pour le journal) */
        table {
            width: 100%;
            border-collapse: collapse;
            margin-top: 20px;
            box-shadow: 0 2px 8px rgba(0, 0, 0, 0.3);
        }
        th, td {
            border: 1px solid #333;
            padding: 10px 12px;
            text-align: left;
        }
        th {
            background-color: #222;
            color: #4CAF50;
        }
        tbody tr:nth-child(even) {
            background-color: #1A1A1A;
        }
        tbody tr:hover {
            background-color: #2A2A2A;
        }
    </style>
</head>
<body>
    <div class="container">
        <h1>Kasperbot Dashboard (v3.0.1)</h1>

        <h2>Signal Actif</h2>
        <div id="active-signal-container">
            <div class="signal-box-pending">Chargement des signaux...</div>
        </div>

        <h2>Surveillance des Symboles</h2>
        <div id="symbol-checklist-container">
            </div>

        <h2>Journal des Trades (Temps Réel)</h2>
        <div id="trade-log-container">
            <table>
                <thead>
                    <tr>
                        <th>Heure</th>
                        <th>Ordre</th>
                        <th>Symbole</th>
                        <th>Type</th>
                        <th>Volume</th>
                        <th>Prix</th>
                        <th>SL</th>
                        <th>TP</th>
                        <th>Raison</th>
                    </tr>
                </thead>
                <tbody id="trade-log-body">
                    </tbody>
            </table>
        </div>
    </div>

    <script>
        // Fonction pour générer les étoiles
        function getStars(rating) {
            return '★'.repeat(rating) + '☆'.repeat(5 - rating);
        }

        // Fonction pour copier le texte du signal
        function copySignalToClipboard(text) {
            navigator.clipboard.writeText(text).then(() => {
                alert('Signal copié dans le presse-papiers !');
            }).catch(err => {
                console.error('Erreur de copie:', err);
            });
        }

        // Fonction pour afficher le signal "en gros" (Mise à jour V3.0)
        async function renderActiveSignal() {
            try {
                const response = await fetch('/api/dashboard/state');
                const data = await response.json();
                const container = document.getElementById('active-signal-container');
                container.innerHTML = ''; // Clear
                
                let signalFound = false;
                for (const symbol in data) {
                    const signal = data[symbol].active_signal;
                    if (signal.is_valid) {
                        signalFound = true;
                        const stars = getStars(signal.rating);
                        const copyText = signal.copy_string;
                        
                        // Ajout de l'attribut onclick pour la copie
                        container.innerHTML += `
                            <div class="signal-box">
                                <h3>Signal Actif (${symbol}) - ${signal.rating}/5</h3>
                                <pre onclick="copySignalToClipboard('${copyText}')" title="Cliquez pour copier">${copyText} (${stars})</pre>
                            </div>
                        `;
                    }
                }
                if (!signalFound) {
                     container.innerHTML = '<div class="signal-box-pending">Aucun signal valide en attente.</div>';
                }
            } catch (error) {
                console.error('Erreur fetch signaux:', error);
            }
        }

        // Fonction pour afficher la liste des checks (Nouvelle V3.0)
        async function renderChecklist() {
            try {
                const response = await fetch('/api/dashboard/state');
                const data = await response.json();
                const container = document.getElementById('symbol-checklist-container');
                container.innerHTML = ''; // Clear
                
                // Trier les symboles par ordre alphabétique pour un affichage stable
                const sortedSymbols = Object.keys(data).sort();
                
                for (const symbol of sortedSymbols) {
                    let checksHtml = '';
                    const checks = data[symbol].checks;
                    
                    // Ordre d'affichage des checks
                    const checkOrder = ['trend', 'session', 'zone', 'poi', 'confirmation', 'risk_sl', 'risk_rr'];
                    
                    for (const checkName of checkOrder) {
                        if (checks[checkName]) {
                            const check = checks[checkName];
                            // Applique une classe CSS basée sur le statut
                            checksHtml += \`
                                <span class="check-item check-${check.status}" title="${check.label}">
                                    ${check.label}
                                </span>\`;
                        }
                    }
                    container.innerHTML += \`<div class="symbol-row"><strong>${symbol}:</strong> ${checksHtml}</div>\`;
                }
            } catch (error) {
                console.error('Erreur fetch checklist:', error);
            }
        }

        // Fonction pour rafraîchir le journal des trades (Logique existante)
        async function fetchTradeLog() {
            try {
                const response = await fetch('/api/trade_log');
                const logData = await response.json();
                const logBody = document.getElementById('trade-log-body');
                logBody.innerHTML = ''; // Clear
                
                // Inverser pour afficher le plus récent en haut
                logData.reverse().forEach(trade => {
                    const row = \`
                        <tr>
                            <td>${trade.timestamp || 'N/A'}</td>
                            <td>${trade.order_id || 'N/A'}</td>
                            <td>${trade.symbol || 'N/A'}</td>
                            <td>${trade.type || 'N/A'}</td>
                            <td>${trade.volume || 'N/A'}</td>
                            <td>${trade.price || 'N/A'}</td>
                            <td>${trade.sl || 'N/A'}</td>
                            <td>${trade.tp || 'N/A'}</td>
                            <td>${trade.reason || 'N/A'}</td>
                        </tr>
                    \`;
                    logBody.innerHTML += row;
                });
            } catch (error) {
                console.error('Erreur de rafraîchissement du journal:', error);
            }
        }

        // Fonction de rafraîchissement globale (Mise à jour V3.0)
        function refreshAllData() {
            renderActiveSignal();
            renderChecklist();
            fetchTradeLog();
        }

        // Lancer la boucle de rafraîchissement
        setInterval(refreshAllData, 3000); // Rafraîchit tout toutes les 3 secondes
        
        // Appel initial
        refreshAllData();

    </script>
</body>
</html>
"""

# --- Endpoints API ---

@app.route('/')
def home():
    """ Sert la page HTML principale du dashboard. """
    return render_template_string(HTML_CONTENT)

# --- NOUVEL ENDPOINT V3.0 ---
@app.route('/api/dashboard/state')
def api_get_dashboard_state():
    """
    Fournit l'état complet de l'analyse (checks et signaux)
    pour tous les symboles.
    """
    try:
        state_data = get_full_state()
        return jsonify(state_data)
    except Exception as e:
        # CORRECTION V3.0.1: 'logger' est maintenant défini
        logger.error(f"[API Server] Erreur dans /api/dashboard/state: {e}")
        return jsonify({"error": str(e)}), 500

# --- ENDPOINT EXISTANT (CONSERVÉ) ---
@app.route('/api/trade_log')
def api_get_trade_log():
    """
    Fournit le journal des trades en mémoire.
    """
    try:
        log_data = get_trade_log()
        return jsonify(log_data)
    except Exception as e:
        # CORRECTION V3.0.1: 'logger' est maintenant défini
        logger.error(f"[API Server] Erreur dans /api/trade_log: {e}")
        return jsonify({"error": str(e)}), 500

# --- Fonction pour démarrer le serveur ---

def run_server(port: int = 5000, debug: bool = False):
    """
    Lance le serveur Flask dans un thread séparé.
    """
    if not app:
        # CORRECTION V3.0.1: 'logger' est maintenant défini
        logger.error("[API Server] Erreur critique: Application Flask non initialisée.")
        return

    # CORRECTION V3.0.1: 'logger' est maintenant défini
    logger.info(f"[API Server] Démarrage du Dashboard sur http://127.0.0.1:{port}")
    
    # Nous utilisons un thread pour que le serveur Flask n'empêche pas
    # le bot de trading (dans main.py) de continuer à s'exécuter.
    server_thread = threading.Thread(
        target=app.run, 
        kwargs={'host': '0.0.0.0', 'port': port, 'debug': debug, 'use_reloader': False}
    )
    server_thread.daemon = True # Permet au programme de se fermer même si le serveur tourne
    server_thread.start()

if __name__ == '__main__':
    # Mode de test pour ce fichier uniquement
    # (Ne sera pas exécuté lorsque importé par main.py)
    
    # CORRECTION V3.0.1: Configuration du logging pour le mode test
    logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
    
    logger.info("Démarrage du serveur API en mode test (standalone)...")
    
    # Simuler un état pour le test
    try:
        from src.shared_state import initialize_symbols, update_symbol_check, update_symbol_signal
        
        # Initialiser l'état de test
        test_symbols = ["EURUSD", "XAUUSD", "BTCUSD"]
        initialize_symbols(test_symbols)
        
        # Simuler des états
        update_symbol_check("EURUSD", "trend", "valid")
        update_symbol_check("EURUSD", "session", "valid")
        update_symbol_check("XAUUSD", "trend", "invalid")
        update_symbol_check("XAUUSD", "session", "valid")
        update_symbol_check("BTCUSD", "trend", "pending")
        
        # Simuler un signal
        update_symbol_signal("EURUSD", {
            "is_valid": True,
            "rating": 4,
            "stars": "★★★★☆",
            "copy_string": "BUY EURUSD 1.15281, SL 1.15200, TP 1.15600"
        })

    except ImportError:
        logger.warning("Mode test: src.shared_state non trouvé. L'état sera vide.")
    
    app.run(host='0.0.0.0', port=5001, debug=True)