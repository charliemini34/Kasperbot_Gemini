# Fichier: src/api/server.py
# Version: 1.1.2 (Restoration HTML + Sugg 10.3)
# Dépendances: Flask, src.shared_state
# DESCRIPTION: Ajout Sugg 10.3 (Endpoint /api/visual_alerts).
#            FIX: Restauration du code HTML embarqué original pour la route '/'.

from flask import Flask, jsonify, request, abort
from src.shared_state import SharedState
import logging
import os

def start_api_server(state: SharedState):
    """Démarre le serveur Flask."""
    
    app = Flask(__name__)
    log = logging.getLogger('werkzeug')
    log.setLevel(logging.ERROR) # Désactiver les logs Flask standards

    # --- RESTAURATION DU CODE HTML ORIGINAL ---
    @app.route('/')
    def index():
        """Sert la page HTML principale (embarquée)."""
        # Ce code HTML est restauré à partir de la version originale du fichier.
        html_content = """
        <!DOCTYPE html>
        <html lang="fr">
        <head>
            <meta charset="UTF-8">
            <meta name="viewport" content="width=device-width, initial-scale=1.0">
            <title>KasperBot Dashboard</title>
            <style>
                body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif; background-color: #121212; color: #E0E0E0; margin: 0; padding: 20px; font-size: 14px; }
                .container { max-width: 1600px; margin: 0 auto; display: grid; grid-template-columns: 3fr 1fr; gap: 20px; }
                .main-content { display: grid; grid-template-rows: auto 1fr; gap: 20px; }
                .sidebar { display: flex; flex-direction: column; gap: 20px; }
                .widget { background-color: #1E1E1E; border: 1px solid #333; border-radius: 8px; padding: 20px; box-shadow: 0 4px 8px rgba(0,0,0,0.2); }
                h1, h2, h3 { color: #FFFFFF; border-bottom: 2px solid #4A90E2; padding-bottom: 5px; margin-top: 0; }
                h1 { font-size: 24px; } h2 { font-size: 20px; } h3 { font-size: 16px; }
                
                /* --- Statut et Contrôles --- */
                #status-widget .content { display: flex; justify-content: space-between; align-items: center; }
                #status-indicator { font-size: 20px; font-weight: bold; padding: 10px 15px; border-radius: 5px; }
                #status-indicator.status-connecte { background-color: #28a745; color: white; }
                #status-indicator.status-deconnecte { background-color: #dc3545; color: white; }
                #status-indicator.status-erreur { background-color: #ffc107; color: #333; }
                #status-message { margin-top: 10px; font-style: italic; color: #AAA; }
                .controls button { background-color: #4A90E2; color: white; border: none; padding: 10px 15px; border-radius: 5px; cursor: pointer; font-size: 14px; margin-left: 10px; }
                .controls button.shutdown { background-color: #dc3545; }
                .controls button:hover { opacity: 0.8; }

                /* --- Alertes Visuelles (Sugg 10) --- */
                #alerts-widget ul { list-style-type: none; padding: 0; margin: 0; max-height: 250px; overflow-y: auto; }
                #alerts-widget li { background-color: #2a2a2a; padding: 10px; border-bottom: 1px solid #333; font-family: "Courier New", Courier, monospace; }
                #alerts-widget li:nth-child(odd) { background-color: #252525; }
                #alerts-widget li:first-child { background-color: #4A90E2; color: white; font-weight: bold; }

                /* --- Positions Ouvertes --- */
                #positions-table { width: 100%; border-collapse: collapse; margin-top: 15px; }
                #positions-table th, #positions-table td { padding: 10px; border: 1px solid #333; text-align: left; }
                #positions-table th { background-color: #333; }
                .profit { color: #28a745; }
                .loss { color: #dc3545; }
                .buy { color: #3498db; }
                .sell { color: #e74c3c; }

                /* --- Symboles et Logs --- */
                .split-widget { display: grid; grid-template-columns: 1fr 1fr; gap: 20px; height: 500px; }
                #symbols-container, #logs-container { height: 100%; overflow-y: auto; background-color: #252525; padding: 15px; border-radius: 5px; }
                #logs-container { font-family: "Courier New", Courier, monospace; font-size: 12px; }
                .log-entry { padding: 3px 0; border-bottom: 1px dotted #444; }
                .symbol-box { border-bottom: 1px solid #444; padding-bottom: 10px; margin-bottom: 10px; }
                .symbol-box h3 { border-bottom: none; padding: 0; }
                .patterns-grid { display: grid; grid-template-columns: repeat(2, 1fr); gap: 5px 10px; font-size: 12px; }
                .pattern-item { white-space: nowrap; }
            </style>
        </head>
        <body>
            <div class="container">
                <div class="main-content">
                    <div class="widget" id="status-widget">
                        <div class="content">
                            <div>
                                <h1>KasperBot Dashboard</h1>
                                <div id="status-indicator" class="status-deconnecte">Démarrage</div>
                                <p id="status-message">Initialisation...</p>
                            </div>
                            <div class="controls">
                                <button id="reload-config-btn">Recharger Config</button>
                                <button id="shutdown-btn" class="shutdown">Arrêt d'Urgence</button>
                            </div>
                        </div>
                    </div>

                    <div class="widget" id="positions-widget">
                        <h2>Positions Ouvertes (<span id="positions-count">0</span>)</h2>
                        <table id="positions-table">
                            <thead>
                                <tr>
                                    <th>Ticket</th> <th>Symbole</th> <th>Type</th> <th>Volume</th>
                                    <th>Prix Ouvert</th> <th>SL</th> <th>TP</th> <th>Profit</th>
                                </tr>
                            </thead>
                            <tbody id="positions-body">
                                </tbody>
                        </table>
                    </div>
                </div>
                
                <div class="sidebar">
                    <div class="widget" id="alerts-widget">
                        <h2>Alertes Signaux</h2>
                        <ul id="alerts-list">
                            <li>Aucune alerte récente.</li>
                        </ul>
                    </div>

                    <div class="widget split-widget">
                        <div id="symbols-container">
                            <h3>Analyse Symboles</h3>
                            <div id="symbols-list">
                                </div>
                        </div>
                        <div id="logs-container">
                            <h3>Logs en direct</h3>
                            <div id="logs-list">
                                </div>
                        </div>
                    </div>
                </div>
            </div>

            <script>
                // --- Intervalle de mise à jour ---
                const UPDATE_INTERVAL = 3000; // 3 secondes

                // --- Fonctions de mise à jour ---

                // Etat principal (Statut, Positions, Symboles)
                async function fetchMainState() {
                    try {
                        const response = await fetch('/api/state');
                        if (!response.ok) throw new Error('Erreur réseau /api/state');
                        const state = await response.json();

                        updateStatus(state.status);
                        updatePositions(state.positions);
                        updateSymbols(state.symbol_data);
                    } catch (error) {
                        console.error("Erreur fetchMainState:", error);
                        updateStatus({ status: "Erreur UI", message: "Impossible de joindre le backend.", is_emergency: true });
                    }
                }

                // Logs
                async function fetchLogs() {
                    try {
                        const response = await fetch('/api/logs');
                        if (!response.ok) throw new Error('Erreur réseau /api/logs');
                        const logs = await response.json();
                        updateLogs(logs);
                    } catch (error) {
                        console.error("Erreur fetchLogs:", error);
                    }
                }

                // Alertes (Sugg 10)
                async function fetchAlerts() {
                    try {
                        const response = await fetch('/api/visual_alerts');
                        if (!response.ok) throw new Error('Erreur réseau /api/visual_alerts');
                        const alerts = await response.json();
                        updateAlerts(alerts);
                    } catch (error) {
                        console.error("Erreur fetchAlerts:", error);
                    }
                }

                // --- Fonctions de rendu ---

                function updateStatus(status) {
                    const indicator = document.getElementById('status-indicator');
                    const message = document.getElementById('status-message');
                    
                    indicator.textContent = status.status || 'Inconnu';
                    message.textContent = status.message || '...';

                    indicator.className = 'status-deconnecte'; // Défaut
                    if (status.is_emergency) {
                        indicator.className = 'status-erreur';
                    } else if (status.status === 'Connecté') {
                        indicator.className = 'status-connecte';
                    }
                }

                function updatePositions(positions) {
                    const body = document.getElementById('positions-body');
                    const count = document.getElementById('positions-count');
                    body.innerHTML = '';
                    count.textContent = positions.length;

                    if (positions.length === 0) {
                        body.innerHTML = '<tr><td colspan="8" style="text-align: center; color: #777;">Aucune position ouverte.</td></tr>';
                        return;
                    }

                    positions.forEach(pos => {
                        const profitClass = pos.profit >= 0 ? 'profit' : 'loss';
                        const typeClass = pos.type === 'BUY' ? 'buy' : 'sell';
                        const row = `
                            <tr>
                                <td>${pos.ticket}</td>
                                <td>${pos.symbol}</td>
                                <td class="${typeClass}">${pos.type}</td>
                                <td>${pos.volume}</td>
                                <td>${pos.price_open.toFixed(5)}</td>
                                <td>${pos.sl.toFixed(5)}</td>
                                <td>${pos.tp.toFixed(5)}</td>
                                <td class="${profitClass}">${pos.profit.toFixed(2)}</td>
                            </tr>
                        `;
                        body.innerHTML += row;
                    });
                }

                function updateSymbols(symbolData) {
                    const container = document.getElementById('symbols-list');
                    container.innerHTML = '';
                    if (Object.keys(symbolData).length === 0) {
                        container.innerHTML = '<p style="color: #777;">Aucun symbole chargé.</p>';
                        return;
                    }

                    for (const [symbol, data] of Object.entries(symbolData)) {
                        let patternsHtml = '';
                        if (data.patterns && Object.keys(data.patterns).length > 0) {
                            for (const [key, val] of Object.entries(data.patterns)) {
                                patternsHtml += `<div class="pattern-item"><strong>${key}:</strong> ${val.status || 'N/A'}</div>`;
                            }
                        } else {
                            patternsHtml = '<div class="pattern-item" style="color: #777;">En attente d'analyse...</div>';
                        }
                        
                        container.innerHTML += `
                            <div class="symbol-box">
                                <h3>${symbol}</h3>
                                <div class="patterns-grid">
                                    ${patternsHtml}
                                </div>
                            </div>
                        `;
                    }
                }

                function updateLogs(logs) {
                    const container = document.getElementById('logs-list');
                    container.innerHTML = logs.map(log => `<div class="log-entry">${log.replace(/\\n/g, '<br>')}</div>`).join('');
                }
                
                function updateAlerts(alerts) {
                    const container = document.getElementById('alerts-list');
                    if (!alerts || alerts.length === 0) {
                        container.innerHTML = '<li style="color: #777;">Aucune alerte récente.</li>';
                        return;
                    }
                    container.innerHTML = alerts.map(alert => `<li>${alert}</li>`).join('');
                }

                // --- Contrôles API ---
                async function postControl(endpoint) {
                    if (!confirm(`Êtes-vous sûr de vouloir exécuter l'action: ${endpoint}?`)) {
                        return;
                    }
                    try {
                        const response = await fetch(endpoint, { method: 'POST' });
                        if (!response.ok) throw new Error('Échec de la commande');
                        const result = await response.json();
                        alert(`Action ${endpoint} exécutée: ${result.status}`);
                    } catch (error) {
                        console.error("Erreur postControl:", error);
                        alert(`Erreur lors de l'exécution de ${endpoint}: ${error.message}`);
                    }
                }

                // --- Démarrage ---
                document.addEventListener('DOMContentLoaded', () => {
                    // Liaisons des boutons
                    document.getElementById('shutdown-btn').addEventListener('click', () => postControl('/api/control/shutdown'));
                    document.getElementById('reload-config-btn').addEventListener('click', () => postControl('/api/control/config_reload'));
                    
                    // Démarrage des mises à jour
                    fetchMainState();
                    fetchLogs();
                    fetchAlerts(); // Sugg 10
                    
                    setInterval(fetchMainState, UPDATE_INTERVAL);
                    setInterval(fetchLogs, UPDATE_INTERVAL * 2); // Logs moins fréquents
                    setInterval(fetchAlerts, UPDATE_INTERVAL); // Alertes à la même fréquence que l'état
                });
            </script>
        </body>
        </html>
        """
        return html_content
    # --- FIN RESTAURATION ---

    @app.route('/api/state')
    def get_state():
        """Fournit l'état complet (statut, positions, etc.)."""
        try:
            full_state = state.get_full_state()
            # Filtrer les credentials MT5 avant de les envoyer
            if 'mt5_credentials' in full_state.get('config', {}):
                del full_state['config']['mt5_credentials']
            return jsonify(full_state)
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    @app.route('/api/logs')
    def get_logs():
        """Fournit les derniers messages de log."""
        try:
            logs = state.get_logs()
            return jsonify(logs)
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    # --- AJOUT SUGGESTION 10.3 ---
    @app.route('/api/visual_alerts')
    def get_visual_alerts():
        """ Récupère les dernières alertes de signaux. """
        try:
            alerts = state.get_visual_alerts()
            return jsonify(alerts)
        except Exception as e:
            return jsonify({"error": str(e)}), 500
    # --- FIN SUGGESTION 10.3 ---

    @app.route('/api/control/shutdown', methods=['POST'])
    def control_shutdown():
        """Arrête le bot."""
        logging.warning("Arrêt demandé via API.")
        state.shutdown()
        return jsonify({"status": "shutdown_initiated"})

    @app.route('/api/control/config_reload', methods=['POST'])
    def control_config_reload():
        """Signale à la boucle principale de recharger config.yaml."""
        logging.info("Rechargement de la configuration demandé via API.")
        state.signal_config_changed()
        return jsonify({"status": "config_reload_signaled"})

    try:
        config = state.config
        host = config.get('api', {}).get('host', '127.0.0.1')
        port = config.get('api', {}).get('port', 5000)
        app.run(host=host, port=port, debug=False, use_reloader=False)
    except Exception as e:
        logging.critical(f"Impossible de démarrer le serveur API: {e}", exc_info=True)
        # Tenter d'informer l'état partagé si possible
        state.update_status("ERREUR API", f"Impossible de démarrer l'API: {e}", is_emergency=True)

    return app

# Permet de tester le serveur API indépendamment
if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO)
    mock_state = SharedState()
    # Ajouter des données mock pour le test
    mock_state.add_log_message("Test: Log API démarré.")
    mock_state.add_visual_alert("Test: Alerte visuelle 1 ★★★☆☆")
    mock_state.update_config({"api": {"host": "127.0.0.1", "port": 5001}})
    logging.info("Démarrage du serveur API en mode test sur port 5001...")
    start_api_server(mock_state)