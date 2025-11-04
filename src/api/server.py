# Fichier: src/api/server.py
# Version: 1.3.1-fix (SMC UI Harmonisation)
# Description: Corrige l'incompatibilité de lecture/écriture avec config.yaml (Bug 8)
#              Corrige l'indicateur de statut (Bug 10)
#              Désactive les fonctions de backtest manquantes (Bug 9)

from flask import Flask, jsonify, render_template_string, request
import yaml
import threading
import logging 
import os
import webbrowser         # ### MODIFICATION ICI ### : Import pour ouvrir le navigateur
from threading import Timer # ### MODIFICATION ICI ### : Pour temporiser l'ouverture

HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="fr">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Dashboard KasperBot v20.0 (SMC)</title>    <script src="https://cdn.tailwindcss.com"></script>
    <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
    <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">
    <style>
        body { background-color: #111827; color: #d1d5db; font-family: 'Inter', sans-serif; }
        .card { background-color: #1f2937; border: 1px solid #374151; border-radius: 0.75rem; }
        .tab-button { background-color: transparent; color: #9ca3af; border-color: transparent; padding: 0.5rem 1rem; border-bottom: 2px solid transparent;}
        .tab-button.active { color: #4f46e5; border-color: #4f46e5; }
        input, select, textarea { background-color: #374151; border: 1px solid #4b5563; border-radius: 0.375rem; padding: 0.5rem 0.75rem; width: 100%; }
        .btn { padding: 0.5rem 1rem; border-radius: 0.375rem; font-weight: 600; transition: background-color 0.2s; cursor: pointer; }
        .btn-primary { background-color: #4f46e5; color: white; } .btn-primary:hover { background-color: #4338ca; }
        .form-checkbox { accent-color: #4f46e5; width: auto; }
        label { font-weight: 500; display: block; margin-bottom: 0.25rem; } /* Style de label amélioré */
        .config-section { border-top: 1px solid #374151; padding-top: 2rem; }
    </style>
</head>
<body class="p-4 sm:p-6 lg:p-8">
    <div class="max-w-7xl mx-auto">
        <header class="flex justify-between items-center mb-6">
            <h1 class="text-2xl sm:text-3xl font-bold text-white">KasperBot <span class="text-sm text-gray-400">v20.0 SMC</span></h1>
            <div id="status-indicator" class="flex items-center space-x-2">
                <div id="status-dot" class="h-4 w-4 rounded-full bg-gray-500"></div><span id="status-text" class="font-medium">Chargement...</span>
            </div>
        </header>
        <div class="mb-6"><div class="border-b border-gray-700"><nav class="-mb-px flex space-x-8">
            <button onclick="showTab('dashboard')" class="tab-button active" id="tab-dashboard">Dashboard</button>
            <button onclick="showTab('config')" class="tab-button" id="tab-config">Configuration</button>
            </nav></div></div>
        <main>
            <div id="content-dashboard" class="tab-content grid grid-cols-1 lg:grid-cols-3 gap-6">
                <div class="lg:col-span-1 space-y-6">
                    <div class="card p-5"><h2 class="text-xl font-semibold text-white mb-4">État du Bot</h2><div id="bot-status-container" class="space-y-3"></div></div>
                    <div class="card p-5" id="learning-suggestions-card" style="display: none;"><h2 class="text-xl font-semibold text-white mb-4">Suggestions d'Analyse</h2><div id="learning-suggestions-container" class="space-y-2 text-sm"></div></div>
                    <div id="patterns-main-container" class="space-y-6"></div>
                </div>
                <div class="lg:col-span-2 space-y-6">
                    <div class="card p-5"><h2 class="text-xl font-semibold text-white mb-4">Positions Ouvertes</h2><div id="positions-container"></div></div>
                    <div class="card p-5"><h2 class="text-xl font-semibold text-white mb-4">Journal d'Événements</h2><div id="logs-container" class="h-96 bg-gray-900 rounded-md p-3 overflow-y-auto text-xs font-mono"></div></div>
                </div>
            </div>
            <div id="content-config" class="tab-content hidden"><div class="card p-6"><form id="config-form" class="space-y-8">
                <div class="config-section"><h3 class="text-lg font-medium text-white">Connexion MetaTrader 5</h3>
                    <div class="mt-4 grid grid-cols-1 gap-y-6 sm:grid-cols-3 sm:gap-x-8">
                        <div><label for="mt5_login">Login MT5</label><input type="text" id="mt5_login"></div>
                        <div><label for="mt5_password">Mot de passe MT5</label><input type="password" id="mt5_password"></div>
                        <div><label for="mt5_server">Serveur MT5</label><input type="text" id="mt5_server"></div>
                    </div>
                </div>
                
                <div class="config-section">
                    <h3 class="text-lg font-medium text-white">Stratégie Smart Money Concepts (SMC)</h3>
                    <div id="smc-config-container" class="mt-4 grid grid-cols-1 gap-y-6 sm:grid-cols-3 sm:gap-x-8">
                        <div>
                            <label for="smc_strategy_htf_timeframe">Timeframe Tendance (HTF)</label>
                            <input type="text" id="smc_strategy_htf_timeframe" placeholder="ex: H4">
                        </div>
                        <div>
                            <label for="smc_strategy_ltf_timeframe">Timeframe Entrée (LTF)</label>
                            <input type="text" id="smc_strategy_ltf_timeframe" placeholder="ex: M15">
                        </div>
                        <div>
                            <label for="smc_strategy_htf_swing_order">Ordre Swing HTF</label>
                            <input type="number" id="smc_strategy_htf_swing_order" placeholder="ex: 10">
                        </div>
                        <div>
                            <label for="smc_strategy_ltf_swing_order">Ordre Swing LTF</label>
                            <input type="number" id="smc_strategy_ltf_swing_order" placeholder="ex: 5">
                        </div>
                    </div>
                </div>
                
                <div class="pt-5"><div class="flex justify-end"><button type="submit" class="btn btn-primary">Sauvegarder et Appliquer</button></div></div>
            </form></div></div>
            
            </main>
    </div>
    <script>
        let equityChart = null;
        function showTab(tabName) {
            document.querySelectorAll('.tab-content').forEach(el => el.classList.add('hidden'));
            document.querySelectorAll('.tab-button').forEach(el => el.classList.remove('active'));
            document.getElementById(`content-${tabName}`).classList.remove('hidden');
            document.getElementById(`tab-${tabName}`).classList.add('active');
        }
        function formatProfit(profit) { return `<span class="${parseFloat(profit) >= 0 ? 'text-green-400' : 'text-red-400'}">${parseFloat(profit).toFixed(2)}</span>`; }
        
        // Fonction fetchAllData (MODIFIÉE pour Bug 10)
        async function fetchAllData() {
            try {
                const res = await fetch('/api/data');
                const data = await res.json();
                
                const statusDot = document.getElementById('status-dot');
                // --- CORRECTION BUG 10 ---
                // Change la couleur en fonction du statut réel
                let statusColor = 'bg-gray-500'; // Défaut (INITIALIZING, STOPPED)
                if (data.status.status === 'RUNNING') {
                    statusColor = 'bg-green-500';
                } else if (data.status.status === 'CRASHED') {
                    statusColor = 'bg-red-500 animate-pulse';
                }
                statusDot.className = `h-4 w-4 rounded-full ${statusColor}`;
                // --- FIN CORRECTION BUG 10 ---
                
                document.getElementById('status-text').textContent = data.status.status;
                document.getElementById('bot-status-container').innerHTML = `<div class="flex justify-between"><span>Status:</span> <strong>${data.status.status}</strong></div><div class="flex justify-between"><span>Message:</span> <em class="text-gray-400 text-right truncate">${data.status.message}</em></div>`;
                
                const suggestionsContainer = document.getElementById('learning-suggestions-container');
                if (data.status.analysis_suggestions && data.status.analysis_suggestions.length > 0) {
                    suggestionsContainer.innerHTML = data.status.analysis_suggestions.map(s => `<p class="text-yellow-400">${s}</p>`).join('');
                    document.getElementById('learning-suggestions-card').style.display = 'block';
                } else {
                    document.getElementById('learning-suggestions-card').style.display = 'none';
                }
                
                const patternsMainContainer = document.getElementById('patterns-main-container');
                patternsMainContainer.innerHTML = '';
                if (data.status.symbol_data && Object.keys(data.status.symbol_data).length > 0) {
                    Object.entries(data.status.symbol_data).forEach(([symbol, symbolData]) => {
                        let patternsHTML = '';
                        if(symbolData.patterns && Object.keys(symbolData.patterns).length > 0){
                            Object.entries(symbolData.patterns).forEach(([name, d]) => {
                                let statusColor = 'text-gray-400';
                                // Logique d'affichage SMC
                                let statusText = d.status || 'En attente...';
                                if (statusText.includes('SIGNAL')) statusColor = 'text-green-400';
                                else if (statusText.includes('ERREUR')) statusColor = 'text-red-400';
                                else if (statusText.includes('En attente OTE')) statusColor = 'text-yellow-400';
                                patternsHTML += `<div class="flex justify-between text-sm"><span class="font-medium">${name}</span><strong class="${statusColor}">${statusText}</strong></div>`;
                            });
                        } else { patternsHTML = '<p class="text-gray-400 text-sm">En attente...</p>'; }
                        patternsMainContainer.innerHTML += `<div class="card p-5"><h2 class="text-xl font-semibold text-white mb-4">Analyse: <span class="text-indigo-400">${symbol}</span></h2><div class="space-y-2">${patternsHTML}</div></div>`;
                    });
                }

                const positionsContainer = document.getElementById('positions-container');
                positionsContainer.innerHTML = data.positions.length > 0 ? `<div class="overflow-x-auto"><table class="w-full text-left"><thead><tr class="border-b border-gray-600 text-sm"><th class="p-2">Symbol</th><th class="p-2">Type</th><th class="p-2">Volume</th><th>Profit</th><th>Magic</th></tr></thead><tbody class="text-sm">${data.positions.map(p => `<tr class="border-b border-gray-700"><td class="p-2 font-bold">${p.symbol}</td><td class="p-2 font-bold ${p.type === 0 ? 'text-blue-400' : 'text-orange-400'}">${p.type === 0 ? 'BUY' : 'SELL'}</td><td class="p-2">${p.volume}</td><td class="p-2 font-semibold">${formatProfit(p.profit)}</td><td class="p-2">${p.magic}</td></tr>`).join('')}</tbody></table></div>` : '<p class="text-gray-400">Aucune position.</p>';
                
                const logsContainer = document.getElementById('logs-container');
                const newLogsHtml = data.logs.map(log => `<p>${log}</p>`).join('');
                if (logsContainer.innerHTML !== newLogsHtml) { logsContainer.innerHTML = newLogsHtml; logsContainer.scrollTop = logsContainer.scrollHeight; }

            } catch (error) { console.error("Erreur de mise à jour:", error); }
        }
        
        // --- loadConfig (MODIFIÉE pour Bug 8) ---
        async function loadConfig() {
            try {
                const res = await fetch('/api/config');
                const config = await res.json();
                
                // --- CORRECTION BUG 8: Utilise la structure config.yaml correcte ---
                // Section MT5
                document.getElementById('mt5_login').value = config.mt5.login;
                document.getElementById('mt5_password').value = config.mt5.password;
                document.getElementById('mt5_server').value = config.mt5.server;
                
                // Section Learning (Legacy) - N'existe pas dans config.yaml
                // document.getElementById('learning_enabled').value = config.learning.enabled.toString();
                
                // Section SMC (Nouvelle)
                if (config.strategy) {
                    // document.getElementById('smc_strategy_enabled').value = config.smc_strategy.enabled.toString(); // N'existe pas
                    document.getElementById('smc_strategy_htf_timeframe').value = config.strategy.htf_timeframe;
                    document.getElementById('smc_strategy_ltf_timeframe').value = config.strategy.ltf_timeframe;
                    document.getElementById('smc_strategy_htf_swing_order').value = config.strategy.htf_swing_order;
                    document.getElementById('smc_strategy_ltf_swing_order').value = config.strategy.ltf_swing_order;
                }
                // --- FIN CORRECTION BUG 8 ---
                
            } catch (error) { console.error("Erreur de chargement de la config:", error); }
        }

        // --- saveConfig (MODIFIÉE pour Bug 8) ---
        async function saveConfig(event) {
            event.preventDefault();
            try {
                const res = await fetch('/api/config');
                let config = await res.json();
                
                // --- CORRECTION BUG 8: Utilise la structure config.yaml correcte ---
                // Section MT5
                config.mt5.login = parseInt(document.getElementById('mt5_login').value);
                config.mt5.password = document.getElementById('mt5_password').value;
                config.mt5.server = document.getElementById('mt5_server').value;
                
                // Section Learning (Legacy) - N'existe pas
                // config.learning.enabled = document.getElementById('learning_enabled').value === 'true';
                
                // Section SMC (Nouvelle)
                if (!config.strategy) { config.strategy = {}; } // Initialiser si n'existe pas
                // config.smc_strategy.enabled = document.getElementById('smc_strategy_enabled').value === 'true'; // N'existe pas
                config.strategy.htf_timeframe = document.getElementById('smc_strategy_htf_timeframe').value;
                config.strategy.ltf_timeframe = document.getElementById('smc_strategy_ltf_timeframe').value;
                config.strategy.htf_swing_order = parseInt(document.getElementById('smc_strategy_htf_swing_order').value);
                config.strategy.ltf_swing_order = parseInt(document.getElementById('smc_strategy_ltf_swing_order').value);
                // --- FIN CORRECTION BUG 8 ---

                await fetch('/api/config', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(config) });
                alert('Configuration sauvegardée ! Le bot va redémarrer.'); // Note: Le main.py gère le redémarrage
            } catch (error) { console.error("Erreur de sauvegarde:", error); }
        }
        
        // --- Fonctions Backtest (inchangées) ---
        async function runBacktest(event) {
            event.preventDefault();
            document.getElementById('backtest-progress-card').classList.remove('hidden');
            document.getElementById('backtest-results-card').classList.add('hidden');
            document.getElementById('run-backtest-btn').disabled = true;
            const params = {
                symbol: document.getElementById('backtest_symbol').value,
                timeframe: document.getElementById('backtest_timeframe').value,
                start_date: document.getElementById('start_date').value,
                end_date: document.getElementById('end_date').value,
                initial_capital: document.getElementById('initial_capital').value,
            };
            await fetch('/api/backtest', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(params) });
            checkBacktestStatus();
        }

        async function checkBacktestStatus() {
            const res = await fetch('/api/backtest/status');
            const data = await res.json();
            if (data.running) {
                document.getElementById('backtest-progress-bar').style.width = data.progress + '%';
                setTimeout(checkBacktestStatus, 1000);
            } else {
                document.getElementById('run-backtest-btn').disabled = false;
                displayBacktestResults(data.results);
            }
        }

        function displayBacktestResults(results) {
            document.getElementById('backtest-progress-card').classList.add('hidden');
            document.getElementById('backtest-results-card').classList.remove('hidden');
            const summaryContainer = document.getElementById('backtest-summary');
            if (results.error) {
                summaryContainer.innerHTML = `<p class="col-span-2 text-red-400">${results.error}</p>`;
                return;
            }
            summaryContainer.innerHTML = `
                <div><p class="text-sm text-gray-400">Profit Final</p><p class="text-2xl font-bold ${results.pnl > 0 ? 'text-green-400' : 'text-red-400'}">${results.pnl.toFixed(2)}</p></div>
                <div><p class="text-sm text-gray-400">Drawdown Max</p><p class="text-2xl font-bold">${results.max_drawdown_percent.toFixed(2)}%</p></div>
                <div><p class="text-sm text-gray-400">Taux de Réussite</p><p class="text-2xl font-bold">${results.win_rate.toFixed(2)}%</p></div>
                <div><p class="text-sm text-gray-400">Trades</p><p class="text-2xl font-bold">${results.total_trades}</p></div>
            `;
            const ctx = document.getElementById('equity-chart').getContext('2d');
            if(equityChart) equityChart.destroy();
            equityChart = new Chart(ctx, {
                type: 'line',
                data: {
                    labels: Array.from(Array(results.equity_curve.length).keys()),
                    datasets: [{ label: 'Évolution du Capital', data: results.equity_curve, borderColor: 'rgb(75, 192, 192)', tension: 0.1, pointRadius: 0 }]
                },
                options: { scales: { x: { display: false } } }
            });
        }
        
        // --- window.onload (MODIFIÉ pour Bug 9) ---
        window.onload = () => {
            setInterval(fetchAllData, 3000);
            fetchAllData();
            loadConfig();
            document.getElementById('config-form').addEventListener('submit', saveConfig);
            // document.getElementById('backtest-form').addEventListener('submit', runBacktest); // CORRECTION BUG 9: Désactivé
            const today = new Date().toISOString().split('T')[0];
            const lastYear = new Date(new Date().setFullYear(new Date().getFullYear() - 1)).toISOString().split('T')[0];
            // CORRECTION BUG 9: Ces champs n'existent plus si le backtest est commenté
            // document.getElementById('end_date').value = today;
            // document.getElementById('start_date').value = lastYear;
        };
    </script>
</body>
</html>
"""

def start_api_server(shared_state):
    
    log = logging.getLogger('root')
    
    app = Flask(__name__)
    logging.getLogger('werkzeug').setLevel(logging.ERROR) 

    @app.route('/')
    def index():
        return render_template_string(HTML_TEMPLATE)

    @app.route('/api/data')
    def get_all_data():
        return jsonify(shared_state.get_all_data())
    
    @app.route('/api/config', methods=['GET', 'POST'])
    def manage_config():
        config_path = 'config.yaml'
        if request.method == 'POST':
            new_config = request.json
            with open(config_path, 'w', encoding='utf-8') as f:
                yaml.dump(new_config, f, sort_keys=False)
            
            # --- CORRECTION BUG 8: Appelle les bonnes fonctions de shared_state ---
            shared_state.set_config(new_config) # Anciennement update_config
            # shared_state.signal_config_changed() # N'existe pas, commenté
            # --- FIN CORRECTION BUG 8 ---
            
            return jsonify({"status": "success"})
        
        config = shared_state.get_config() 
        return jsonify(config)

    # --- CORRECTION BUG 9: Désactivation des routes de backtest ---
    # @app.route('/api/backtest', methods=['POST'])
    # def handle_backtest():
    #     from src.backtest.backtester import Backtester # Import cassé
    #     if shared_state.get_backtest_status()['running']: 
    #         return jsonify({"error": "Un backtest est déjà en cours."}), 400
    #     
    #     bt = Backtester(shared_state)
    #     params = request.json
    #     current_config = shared_state.get_config()
    #     
    #     threading.Thread(
    #         target=bt.run, 
    #         args=(
    #             params['symbol'], params['timeframe'],
    #             params['start_date'], params['end_date'], 
    #             params['initial_capital'], current_config
    #         ), 
    #         daemon=True
    #     ).start()
    #     return jsonify({"status": "Backtest démarré."})

    # @app.route('/api/backtest/status')
    # def get_backtest_status(): 
    #     return jsonify(shared_state.get_backtest_status())
    # --- FIN CORRECTION BUG 9 ---
    
    config = shared_state.get_config()
    host = config.get('api', {}).get('host', '127.0.0.1')
    port = config.get('api', {}).get('port', 5000)
    
    try:
        log.info(f"Tentative de démarrage du serveur API Flask sur http://{host}:{port}...")
        
        # ### MODIFICATION ICI ### : Ouvre le navigateur après 1 seconde
        url = f"http://{host}:{port}"
        Timer(1, lambda: webbrowser.open_new_tab(url)).start()
        # ### FIN MODIFICATION ###
        
        app.run(host=host, port=port, debug=False, use_reloader=False)
    except Exception as e:
        log.critical(f"ÉCHEC CRITIQUE DU SERVEUR API: {e}", exc_info=True)