# Fichier: src/api/server.py

from flask import Flask, jsonify, render_template_string, request
import yaml
import threading
import logging
import os

HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="fr">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Dashboard KasperBot v9.0</title>
    <script src="https://cdn.tailwindcss.com"></script>
    <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
    <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">
    <style>
        body { background-color: #111827; color: #d1d5db; font-family: 'Inter', sans-serif; }
        .card { background-color: #1f2937; border: 1px solid #374151; border-radius: 0.75rem; }
        .tab-button { background-color: transparent; color: #9ca3af; border-color: transparent; }
        .tab-button.active { color: #4f46e5; border-color: #4f46e5; }
        input, select, textarea { background-color: #374151; border: 1px solid #4b5563; border-radius: 0.375rem; padding: 0.5rem 0.75rem; width: 100%; }
        .btn { padding: 0.5rem 1rem; border-radius: 0.375rem; font-weight: 600; transition: background-color 0.2s; cursor: pointer; }
        .btn-primary { background-color: #4f46e5; color: white; } .btn-primary:hover { background-color: #4338ca; }
        .form-checkbox { accent-color: #4f46e5; width: auto; }
        label { font-weight: 500; }
        .config-section { border-top: 1px solid #374151; padding-top: 2rem; }
    </style>
</head>
<body class="p-4 sm:p-6 lg:p-8">
    <div class="max-w-7xl mx-auto">
        <header class="flex justify-between items-center mb-6">
            <h1 class="text-2xl sm:text-3xl font-bold text-white">KasperBot <span class="text-sm text-gray-400">v9.0 (Kasper-Learn)</span></h1>
            <div id="status-indicator" class="flex items-center space-x-2">
                <div id="status-dot" class="h-4 w-4 rounded-full bg-gray-500"></div><span id="status-text" class="font-medium">Chargement...</span>
            </div>
        </header>
        <div class="mb-6"><div class="border-b border-gray-700"><nav class="-mb-px flex space-x-8">
            <button onclick="showTab('dashboard')" class="tab-button active whitespace-nowrap py-4 px-1 border-b-2 font-medium text-sm" id="tab-dashboard">Dashboard</button>
            <button onclick="showTab('config')" class="tab-button whitespace-nowrap py-4 px-1 border-b-2 font-medium text-sm" id="tab-config">Configuration</button>
            <button onclick="showTab('backtest')" class="tab-button whitespace-nowrap py-4 px-1 border-b-2 font-medium text-sm" id="tab-backtest">Backtesting</button>
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
                
                <div class="config-section"><h3 class="text-lg font-medium text-white">Moteur d'Apprentissage (Kasper-Learn)</h3>
                    <div class="mt-4 grid grid-cols-1 gap-y-6 sm:grid-cols-2 sm:gap-x-8">
                        <div><label for="learning_enabled">Mode Automatisé</label><select id="learning_enabled"><option value="true">Activé</option><option value="false">Désactivé (Suggestions)</option></select></div>
                    </div>
                </div>

                <div class="config-section"><h3 class="text-lg font-medium text-white">Gestion du Risque</h3>
                    <div class="mt-4 grid grid-cols-1 gap-y-6 sm:grid-cols-3 sm:gap-x-8">
                        <div><label for="risk_per_trade">Risque par Trade (%)</label><input type="number" id="risk_per_trade" step="0.01"></div>
                        <div class="flex items-center mt-6"><input id="breakeven_enabled" type="checkbox" class="form-checkbox h-4 w-4 rounded"><label for="breakeven_enabled" class="ml-2">Activer le Breakeven</label></div>
                        <div class="flex items-center mt-6"><input id="trailing_stop_atr_enabled" type="checkbox" class="form-checkbox h-4 w-4 rounded"><label for="trailing_stop_atr_enabled" class="ml-2">Activer le Trailing Stop (ATR)</label></div>
                    </div>
                </div>
                
                <div class="config-section"><h3 class="text-lg font-medium text-white">Détection de Patterns SMC</h3><div id="patterns-config-container" class="mt-4 grid grid-cols-2 sm:grid-cols-3 gap-4"></div></div>

                <div class="pt-5"><div class="flex justify-end"><button type="submit" class="btn btn-primary">Sauvegarder et Appliquer</button></div></div>
            </form></div></div>
            
            <div id="content-backtest" class="tab-content hidden">
                 <div class="grid grid-cols-1 lg:grid-cols-3 gap-6">
                    <div class="lg:col-span-1"><div class="card p-6"><h2 class="text-xl font-semibold text-white mb-4">Paramètres du Backtest</h2><form id="backtest-form" class="space-y-4">
                        <div><label for="start_date">Date de début</label><input type="date" id="start_date" class="mt-1 block w-full"></div>
                        <div><label for="end_date">Date de fin</label><input type="date" id="end_date" class="mt-1 block w-full"></div>
                        <div><label for="initial_capital">Capital Initial</label><input type="number" id="initial_capital" value="10000" class="mt-1 block w-full"></div>
                        <button type="submit" id="run-backtest-btn" class="w-full bg-indigo-600 hover:bg-indigo-700 text-white font-bold py-3 rounded-md mt-4">Lancer le Backtest</button>
                    </form></div></div>
                    <div class="lg:col-span-2">
                        <div id="backtest-results-card" class="card p-6 hidden"><h2 class="text-xl font-semibold text-white mb-4">Résultats du Backtest</h2>
                            <div id="backtest-summary" class="grid grid-cols-2 gap-4 text-center mb-4"></div><div id="backtest-chart-container"><canvas id="equity-chart"></canvas></div></div>
                        <div id="backtest-progress-card" class="card p-6 hidden"><h2 class="text-xl font-semibold text-white mb-4">Backtest en cours...</h2>
                            <div class="w-full bg-gray-600 rounded-full h-4"><div id="backtest-progress-bar" class="bg-blue-500 h-4 rounded-full w-0"></div></div><p id="backtest-progress-text" class="text-center mt-2">0%</p></div>
                    </div>
                </div>
            </div>
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
        
        async function fetchAllData() {
            try {
                const res = await fetch('/api/data');
                const data = await res.json();
                const statusDot = document.getElementById('status-dot');
                statusDot.className = `h-4 w-4 rounded-full ${data.status.is_emergency ? 'bg-red-500 animate-pulse' : 'bg-green-500'}`;
                document.getElementById('status-text').textContent = data.status.status;
                document.getElementById('bot-status-container').innerHTML = `<div class="flex justify-between"><span>Status:</span> <strong>${data.status.status}</strong></div><div class="flex justify-between"><span>Message:</span> <em class="text-gray-400 text-right truncate">${data.status.message}</em></div>`;
                
                const suggestionsContainer = document.getElementById('learning-suggestions-container');
                if (data.status.analysis_suggestions && data.status.analysis_suggestions.length > 0) {
                    suggestionsContainer.innerHTML = data.status.analysis_suggestions.map(s => `<p class="text-yellow-400">${s}</p>`).join('');
                    document.getElementById('learning-suggestions-card').style.display = 'block';
                } else {
                    suggestionsContainer.innerHTML = '';
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
                                const statusText = d.status || 'En attente...';
                                if (statusText.includes('CONFIRMÉ')) statusColor = 'text-green-400';
                                else if (statusText.includes('INVALIDÉ')) statusColor = 'text-red-400';
                                else if (statusText.includes('Signal BUY')) statusColor = 'text-blue-400';
                                else if (statusText.includes('Signal SELL')) statusColor = 'text-orange-400';
                                patternsHTML += `<div class="flex justify-between text-sm"><span class="font-medium">${name}</span><strong class="${statusColor}">${statusText}</strong></div>`;
                            });
                        } else { patternsHTML = '<p class="text-gray-400 text-sm">En attente...</p>'; }
                        patternsMainContainer.innerHTML += `<div class="card p-5"><h2 class="text-xl font-semibold text-white mb-4">Analyse SMC: <span class="text-indigo-400">${symbol}</span></h2><div class="space-y-2">${patternsHTML}</div></div>`;
                    });
                }
                const positionsContainer = document.getElementById('positions-container');
                positionsContainer.innerHTML = data.positions.length > 0 ? `<div class="overflow-x-auto"><table class="w-full text-left"><thead><tr class="border-b border-gray-600 text-sm"><th class="p-2">Symbol</th><th class="p-2">Type</th><th class="p-2">Volume</th><th>Profit</th><th>Magic</th></tr></thead><tbody class="text-sm">${data.positions.map(p => `<tr class="border-b border-gray-700"><td class="p-2 font-bold">${p.symbol}</td><td class="p-2 font-bold ${p.type === 0 ? 'text-blue-400' : 'text-orange-400'}">${p.type === 0 ? 'BUY' : 'SELL'}</td><td class="p-2">${p.volume}</td><td class="p-2 font-semibold">${formatProfit(p.profit)}</td><td class="p-2">${p.magic}</td></tr>`).join('')}</tbody></table></div>` : '<p class="text-gray-400">Aucune position.</p>';
                const logsContainer = document.getElementById('logs-container');
                const newLogsHtml = data.logs.map(log => `<p>${log}</p>`).join('');
                if (logsContainer.innerHTML !== newLogsHtml) { logsContainer.innerHTML = newLogsHtml; logsContainer.scrollTop = logsContainer.scrollHeight; }
            } catch (error) { console.error("Erreur de mise à jour:", error); }
        }
        
        async function loadConfig() {
            try {
                const res = await fetch('/api/config');
                const config = await res.json();

                document.getElementById('learning_enabled').value = config.learning.enabled.toString();
                document.getElementById('risk_per_trade').value = (config.risk_management.risk_per_trade * 100).toFixed(2);
                document.getElementById('breakeven_enabled').checked = config.risk_management.breakeven.enabled;
                document.getElementById('trailing_stop_atr_enabled').checked = config.risk_management.trailing_stop_atr.enabled;

                const patternsContainer = document.getElementById('patterns-config-container');
                patternsContainer.innerHTML = '';
                Object.keys(config.pattern_detection).forEach(name => {
                    patternsContainer.innerHTML += `<div class="flex items-center"><input id="pattern_${name}" type="checkbox" class="form-checkbox h-4 w-4 rounded" ${config.pattern_detection[name] ? 'checked' : ''}><label for="pattern_${name}" class="ml-2">${name}</label></div>`;
                });
            } catch (error) { console.error("Erreur de chargement de la config:", error); }
        }

        async function saveConfig(event) {
            event.preventDefault();
            try {
                const res = await fetch('/api/config');
                let config = await res.json();
                
                config.learning.enabled = document.getElementById('learning_enabled').value === 'true';
                config.risk_management.risk_per_trade = parseFloat(document.getElementById('risk_per_trade').value) / 100;
                config.risk_management.breakeven.enabled = document.getElementById('breakeven_enabled').checked;
                config.risk_management.trailing_stop_atr.enabled = document.getElementById('trailing_stop_atr_enabled').checked;

                Object.keys(config.pattern_detection).forEach(name => {
                    const checkbox = document.getElementById(`pattern_${name}`);
                    if(checkbox) config.pattern_detection[name] = checkbox.checked;
                });

                await fetch('/api/config', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(config) });
                alert('Configuration sauvegardée !');
            } catch (error) { console.error("Erreur de sauvegarde:", error); alert("Erreur lors de la sauvegarde."); }
        }
        
        // ... (partie backtest inchangée)
        async function runBacktest(event) {
            event.preventDefault();
            document.getElementById('backtest-progress-card').classList.remove('hidden');
            document.getElementById('backtest-results-card').classList.add('hidden');
            const btn = document.getElementById('run-backtest-btn');
            btn.disabled = true; btn.textContent = 'En cours...';
            const params = { start_date: document.getElementById('start_date').value, end_date: document.getElementById('end_date').value, initial_capital: document.getElementById('initial_capital').value };
            await fetch('/api/backtest', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(params) });
            checkBacktestStatus();
        }
        async function checkBacktestStatus() {
            const res = await fetch('/api/backtest/status');
            const data = await res.json();
            document.getElementById('backtest-progress-bar').style.width = data.progress + '%';
            document.getElementById('backtest-progress-text').textContent = Math.round(data.progress) + '%';
            if (data.running) {
                setTimeout(checkBacktestStatus, 1500);
            } else {
                displayBacktestResults(data.results);
                const btn = document.getElementById('run-backtest-btn');
                btn.disabled = false; btn.textContent = 'Lancer le Backtest';
            }
        }
        function displayBacktestResults(results) {
            document.getElementById('backtest-progress-card').classList.add('hidden');
            document.getElementById('backtest-results-card').classList.remove('hidden');
            if (!results || results.error) {
                document.getElementById('backtest-summary').innerHTML = `<p class="col-span-2 text-red-400">${results ? results.error : 'Erreur inconnue.'}</p>`;
                return;
            }
            document.getElementById('backtest-summary').innerHTML = `
                <div><p class="text-sm text-gray-400">Profit Final</p><p class="text-2xl font-bold ${results.pnl > 0 ? 'text-green-400' : 'text-red-400'}">${results.pnl.toFixed(2)}</p></div>
                <div><p class="text-sm text-gray-400">Drawdown Max</p><p class="text-2xl font-bold">${results.max_drawdown_percent.toFixed(2)}%</p></div>
                <div><p class="text-sm text-gray-400">Taux de Réussite</p><p class="text-2xl font-bold">${results.win_rate.toFixed(2)}%</p></div>
                <div><p class="text-sm text-gray-400">Nb. Trades</p><p class="text-2xl font-bold">${results.total_trades}</p></div>`;
            const ctx = document.getElementById('equity-chart').getContext('2d');
            if(equityChart) equityChart.destroy();
            equityChart = new Chart(ctx, { type: 'line', data: { labels: Array.from(Array(results.equity_curve.length).keys()), datasets: [{ label: 'Évolution du Capital', data: results.equity_curve, borderColor: '#4f46e5', tension: 0.1, pointRadius: 0 }] }, options: { scales: { x: { display: false } } } });
        }
        
        window.onload = () => {
            setInterval(fetchAllData, 3000);
            fetchAllData();
            loadConfig();
            document.getElementById('config-form').addEventListener('submit', saveConfig);
            document.getElementById('backtest-form').addEventListener('submit', runBacktest);
            const today = new Date().toISOString().split('T')[0];
            const lastYear = new Date(new Date().setFullYear(new Date().getFullYear() - 1)).toISOString().split('T')[0];
            document.getElementById('end_date').value = today;
            document.getElementById('start_date').value = lastYear;
        };
    </script>
</body>
</html>
"""

def start_api_server(shared_state):
    app = Flask(__name__)
    logging.getLogger('werkzeug').setLevel(logging.ERROR)

    def load_yaml(filepath):
        with open(filepath, 'r', encoding='utf-8') as f:
            return yaml.safe_load(f)

    def write_yaml(filepath, data):
        with open(filepath, 'w', encoding='utf-8') as f:
            yaml.dump(data, f, sort_keys=False)

    @app.route('/')
    def index():
        return render_template_string(HTML_TEMPLATE)

    @app.route('/api/data')
    def get_all_data():
        return jsonify(shared_state.get_all_data())
    
    @app.route('/api/config', methods=['GET', 'POST'])
    def manage_config():
        if request.method == 'POST':
            new_config = request.json
            write_yaml('config.yaml', new_config)
            shared_state.update_config(new_config)
            shared_state.signal_config_changed()
            return jsonify({"status": "success"})
        return jsonify(shared_state.get_config())

    @app.route('/api/backtest', methods=['POST'])
    def handle_backtest():
        from src.backtest.backtester import Backtester
        if shared_state.get_backtest_status()['running']: 
            return jsonify({"error": "Un backtest est déjà en cours."}), 400
        bt = Backtester(shared_state)
        params = request.json
        threading.Thread(
            target=bt.run, 
            args=(params['start_date'], params['end_date'], params['initial_capital']), 
            daemon=True
        ).start()
        return jsonify({"status": "Backtest démarré."})

    @app.route('/api/backtest/status')
    def get_backtest_status(): 
        return jsonify(shared_state.get_backtest_status())
    
    config = shared_state.get_config()
    host = config.get('api', {}).get('host', '127.0.0.1')
    port = config.get('api', {}).get('port', 5000)
    
    app.run(host=host, port=port, debug=False, use_reloader=False)