from flask import Flask, jsonify, render_template_string, request
import yaml
import threading
import logging
from src.backtest.backtester import Backtester
import os

# Les profils par défaut sont gardés en mémoire pour permettre la réinitialisation
DEFAULT_PROFILES = {
    'equilibre_pro_tendance': {'TREND': 0.35, 'SMC': 0.30, 'MEAN_REV': 0.10, 'VOL_BRK': 0.15, 'LONDON_BRK': 0.10},
    'agressif_chasseur_breakout': {'TREND': 0.15, 'SMC': 0.20, 'MEAN_REV': 0.05, 'VOL_BRK': 0.40, 'LONDON_BRK': 0.20},
    'conservateur_suiveur_tendance': {'TREND': 0.50, 'SMC': 0.30, 'MEAN_REV': 0.05, 'VOL_BRK': 0.10, 'LONDON_BRK': 0.05},
    'custom': {'TREND': 0.20, 'SMC': 0.20, 'MEAN_REV': 0.20, 'VOL_BRK': 0.20, 'LONDON_BRK': 0.20}
}

HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="fr">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Dashboard Bot Trading XAUUSD v3.0</title>
    <script src="https://cdn.tailwindcss.com"></script>
    <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
    <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">
    <style>
        body { background-color: #111827; color: #d1d5db; font-family: 'Inter', sans-serif; }
        .card { background-color: #1f2937; border: 1px solid #374151; border-radius: 0.75rem; }
        .tab-button { background-color: transparent; color: #9ca3af; border-color: transparent; }
        .tab-button.active { color: #4f46e5; border-color: #4f46e5; }
        .kill-switch { background-color: #b91c1c; } .kill-switch:hover { background-color: #991b1b; }
        input, select { background-color: #374151; border: 1px solid #4b5563; border-radius: 0.375rem; padding: 0.5rem 0.75rem; }
        .btn { padding: 0.5rem 1rem; border-radius: 0.375rem; font-weight: 600; transition: background-color 0.2s; cursor: pointer; }
        .btn-primary { background-color: #4f46e5; color: white; } .btn-primary:hover { background-color: #4338ca; }
        .btn-secondary { background-color: #374151; color: #d1d5db; } .btn-secondary:hover { background-color: #4b5563; }
    </style>
</head>
<body class="p-4 sm:p-6 lg:p-8">
    <div class="max-w-7xl mx-auto">
        <header class="flex justify-between items-center mb-6">
            <h1 class="text-2xl sm:text-3xl font-bold text-white"><span class="text-amber-400">XAUUSD</span> Pro Trading Bot <span class="text-sm text-gray-400">v3.0</span></h1>
            <div id="status-indicator" class="flex items-center space-x-2">
                <div id="status-dot" class="h-4 w-4 rounded-full bg-gray-500"></div><span id="status-text" class="font-medium">Chargement...</span>
            </div>
        </header>

        <div class="mb-6"><div class="border-b border-gray-700"><nav class="-mb-px flex space-x-8" aria-label="Tabs">
            <button onclick="showTab('dashboard')" class="tab-button active whitespace-nowrap py-4 px-1 border-b-2 font-medium text-sm" id="tab-dashboard">Dashboard</button>
            <button onclick="showTab('config')" class="tab-button whitespace-nowrap py-4 px-1 border-b-2 font-medium text-sm" id="tab-config">Configuration</button>
            <button onclick="showTab('backtest')" class="tab-button whitespace-nowrap py-4 px-1 border-b-2 font-medium text-sm" id="tab-backtest">Backtesting</button>
        </nav></div></div>

        <main>
            <div id="content-dashboard" class="tab-content grid grid-cols-1 lg:grid-cols-3 gap-6">
                <div class="lg:col-span-1 space-y-6">
                    <div class="card p-5"><h2 class="text-xl font-semibold text-white mb-4">État du Bot</h2><div class="space-y-3">
                        <div class="flex justify-between"><span>Status:</span> <strong id="bot-status">...</strong></div>
                        <div class="flex justify-between"><span>Message:</span> <em id="bot-message" class="text-gray-400 text-right truncate">...</em></div>
                        <div class="flex justify-between"><span>PnL Journalier:</span> <strong id="bot-pnl">...</strong></div>
                    </div><button id="kill-switch-btn" class="kill-switch w-full mt-6 text-white font-bold py-3 rounded-lg shadow-lg">ARRÊT D'URGENCE</button></div>
                    <div class="card p-5"><h2 class="text-xl font-semibold text-white mb-4">Analyse des Stratégies</h2><div id="scores-container" class="space-y-2"><p class="text-gray-400">En attente...</p></div></div>
                </div>
                <div class="lg:col-span-2 space-y-6">
                     <div class="card p-5"><h2 class="text-xl font-semibold text-white mb-4">Positions Ouvertes</h2><div class="overflow-x-auto"><table class="w-full text-left"><thead><tr class="border-b border-gray-600 text-sm"><th class="p-2">Ticket</th><th>Type</th><th>Volume</th><th>Entrée</th><th>SL</th><th>TP</th><th>Profit</th></tr></thead><tbody id="positions-table" class="text-sm"></tbody></table></div></div>
                    <div class="card p-5"><h2 class="text-xl font-semibold text-white mb-4">Journal d'Événements</h2><div id="logs-container" class="h-64 bg-gray-900 rounded-md p-3 overflow-y-auto text-xs font-mono"></div></div>
                </div>
            </div>

            <div id="content-config" class="tab-content hidden"><div class="card p-6"><form id="config-form" class="space-y-8">
                <div><h3 class="text-lg font-medium text-white">Profil de Stratégie</h3><div class="mt-4 flex items-center gap-4">
                    <select id="profile_selector" class="block w-full sm:w-auto"></select>
                    <button type="button" id="reset-profile-btn" class="btn btn-secondary">Réinitialiser ce profil</button>
                </div></div>
                <div><h3 class="text-lg font-medium text-white">Poids des Stratégies (Profil Actif)</h3><div id="strategy-weights-container" class="mt-4 grid grid-cols-1 gap-y-6 sm:grid-cols-3 sm:gap-x-8"></div></div>
                <div><h3 class="text-lg font-medium text-white">Paramètres Généraux</h3><div class="mt-4 grid grid-cols-1 gap-y-6 sm:grid-cols-2 sm:gap-x-8">
                    <div><label for="execution_threshold">Seuil de Déclenchement (%)</label><input type="number" id="execution_threshold" class="mt-1 block w-full" min="1" max="100"></div>
                    <div><label for="live_trading_enabled">Mode de Trading</label><select id="live_trading_enabled" class="mt-1 block w-full"><option value="true">Activé (Réel)</option><option value="false" selected>Désactivé (Simulation)</option></select></div>
                </div></div>
                <div><h3 class="text-lg font-medium text-white">Apprentissage et IA</h3><div class="mt-4 grid grid-cols-1 gap-y-6 sm:grid-cols-2 sm:gap-x-8">
                    <div><label for="auto_optimization_enabled">Auto-Optimisation des Poids</label><select id="auto_optimization_enabled" class="mt-1 block w-full"><option value="true">Activé</option><option value="false">Désactivé</option></select></div>
                    <div><label for="ai_confirmation_enabled">Confirmation par IA</label><select id="ai_confirmation_enabled" class="mt-1 block w-full"><option value="true">Activé</option><option value="false">Désactivé</option></select></div>
                    <p class="text-sm text-gray-400 mt-2 sm:col-span-2">L'auto-optimisation modifie le profil <span class="font-bold text-amber-400">'Custom'</span>. La confirmation par IA nécessite une clé API Gemini.</p>
                </div></div>
                <div class="pt-5"><div class="flex justify-end"><button type="submit" class="btn btn-primary">Sauvegarder et Appliquer</button></div></div>
            </form></div></div>

            <div id="content-backtest" class="tab-content hidden">
                 <div class="grid grid-cols-1 lg:grid-cols-3 gap-6">
                    <div class="lg:col-span-1"><div class="card p-6"><h2 class="text-xl font-semibold text-white mb-4">Paramètres du Backtest</h2><form id="backtest-form" class="space-y-4">
                        <div><label for="start_date">Date de début</label><input type="date" id="start_date" class="mt-1 block w-full"></div>
                        <div><label for="end_date">Date de fin</label><input type="date" id="end_date" class="mt-1 block w-full"></div>
                        <div><label for="initial_capital">Capital Initial</label><input type="number" id="initial_capital" value="10000" class="mt-1 block w-full"></div>
                        <button type="submit" id="run-backtest-btn" class="w-full bg-green-600 hover:bg-green-700 text-white font-bold py-3 rounded-md mt-4">Lancer le Backtest</button>
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

        async function fetchAllData() {
            try {
                const res = await fetch('/api/data');
                const data = await res.json();
                updateDashboard(data);
            } catch (error) { console.error("Erreur de mise à jour:", error); }
        }
        
        function updateDashboard(data) {
            document.getElementById('bot-status').textContent = data.status.status;
            document.getElementById('bot-message').textContent = data.status.message;
            document.getElementById('bot-pnl').innerHTML = formatProfit(data.status.daily_pnl);
            const statusDot = document.getElementById('status-dot');
            const statusText = document.getElementById('status-text');
            statusDot.className = `h-4 w-4 rounded-full ${data.status.is_emergency ? 'bg-red-500 animate-pulse' : 'bg-green-500'}`;
            statusText.textContent = data.status.status;
            const scoresContainer = document.getElementById('scores-container');
            scoresContainer.innerHTML = '';
            if (Object.keys(data.status.scores).length > 0) {
                 Object.entries(data.status.scores).forEach(([name, d]) => {
                    const dirColor = d.direction === 'BUY' ? 'bg-green-600' : (d.direction === 'SELL' ? 'bg-red-600' : 'bg-gray-600');
                    scoresContainer.innerHTML += `<div class="flex items-center justify-between text-sm"><span class="font-medium">${name}</span><div class="flex items-center space-x-2"><span class="w-12 text-right font-semibold">${d.score.toFixed(1)}</span><div class="w-32 bg-gray-600 rounded-full h-2"><div class="h-2 rounded-full ${dirColor}" style="width: ${d.score}%"></div></div></div></div>`;
                });
            } else { scoresContainer.innerHTML = '<p class="text-gray-400 text-sm">En attente...</p>'; }
            const positionsTable = document.getElementById('positions-table');
            positionsTable.innerHTML = data.positions.length > 0 ? data.positions.map(p => `
                <tr class="border-b border-gray-700 hover:bg-gray-700/50"><td class="p-2">${p.ticket}</td><td class="p-2 font-bold ${p.type === 0 ? 'text-blue-400' : 'text-orange-400'}">${p.type === 0 ? 'BUY' : 'SELL'}</td><td class="p-2">${p.volume}</td><td class="p-2">${p.price_open.toFixed(3)}</td><td class="p-2">${p.sl.toFixed(3)}</td><td class="p-2">${p.tp.toFixed(3)}</td><td class="p-2 font-semibold">${formatProfit(p.profit)}</td></tr>`).join('') : '<tr><td colspan="7" class="text-center p-4 text-gray-400">Aucune position.</td></tr>';
            const logsContainer = document.getElementById('logs-container');
            const newLogsHtml = data.logs.map(log => `<p>${log.replace(/\\n/g, '<br>')}</p>`).join('');
            if (logsContainer.innerHTML !== newLogsHtml) {
                logsContainer.innerHTML = newLogsHtml;
                logsContainer.scrollTop = logsContainer.scrollHeight;
            }
        }

        async function loadConfigAndProfiles() {
            const [configRes, profilesRes] = await Promise.all([fetch('/api/config'), fetch('/api/profiles')]);
            const config = await configRes.json();
            const profiles = await profilesRes.json();
            const profileSelector = document.getElementById('profile_selector');
            profileSelector.innerHTML = '';
            Object.keys(profiles).forEach(name => {
                const option = document.createElement('option');
                option.value = name;
                option.textContent = name.replace(/_/g, ' ').replace(/\\b\\w/g, l => l.toUpperCase());
                profileSelector.appendChild(option);
            });
            profileSelector.value = config.trading_logic.active_profile;
            applyProfileToForm(config.trading_logic.active_profile, profiles);
            document.getElementById('execution_threshold').value = config.trading_logic.execution_threshold;
            document.getElementById('live_trading_enabled').value = config.trading_settings.live_trading_enabled.toString();
            document.getElementById('auto_optimization_enabled').value = config.learning.auto_optimization_enabled.toString();
            document.getElementById('ai_confirmation_enabled').value = config.learning.ai_confirmation_enabled.toString();
        }

        function applyProfileToForm(profileName, profiles) {
            const weights = profiles[profileName];
            const weightsContainer = document.getElementById('strategy-weights-container');
            weightsContainer.innerHTML = '';
            Object.entries(weights).forEach(([name, value]) => {
                weightsContainer.innerHTML += `<div><label for="weight_${name}" class="block text-sm font-medium">${name}</label><input type="number" id="weight_${name}" value="${value}" class="mt-1 block w-full" step="0.01" oninput="markProfileAsCustom()"></div>`;
            });
        }
        
        function markProfileAsCustom() { document.getElementById('profile_selector').value = 'custom'; }

        async function saveConfig(event) {
            event.preventDefault();
            const configRes = await fetch('/api/config');
            const currentConfig = await configRes.json();
            const activeProfile = document.getElementById('profile_selector').value;
            currentConfig.trading_logic.active_profile = activeProfile;
            currentConfig.trading_logic.execution_threshold = parseInt(document.getElementById('execution_threshold').value);
            currentConfig.trading_settings.live_trading_enabled = document.getElementById('live_trading_enabled').value === 'true';
            currentConfig.learning.auto_optimization_enabled = document.getElementById('auto_optimization_enabled').value === 'true';
            currentConfig.learning.ai_confirmation_enabled = document.getElementById('ai_confirmation_enabled').value === 'true';
            const newWeights = {};
            Object.keys(DEFAULT_PROFILES.custom).forEach(name => {
                const input = document.getElementById(`weight_${name}`);
                if (input) newWeights[name] = parseFloat(input.value);
            });
            await fetch(`/api/profiles/${activeProfile}`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(newWeights) });
            await fetch('/api/config', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(currentConfig) });
            alert('Configuration sauvegardée !');
        }
        
        async function resetProfile() {
            const profileToReset = document.getElementById('profile_selector').value;
            if (confirm(`Êtes-vous sûr de vouloir réinitialiser le profil "${profileToReset}" ?`)) {
                await fetch(`/api/profiles/reset/${profileToReset}`, { method: 'POST' });
                await loadConfigAndProfiles();
                alert(`Profil "${profileToReset}" réinitialisé.`);
            }
        }
        
        async function onProfileChange() {
            const selectedProfile = document.getElementById('profile_selector').value;
            const res = await fetch('/api/profiles');
            const profiles = await res.json();
            applyProfileToForm(selectedProfile, profiles);
        }

        function formatProfit(profit) { return `<span class="${parseFloat(profit) >= 0 ? 'text-green-400' : 'text-red-400'}">${parseFloat(profit).toFixed(2)}</span>`; }
        async function runBacktest(event) { event.preventDefault(); document.getElementById('backtest-progress-card').classList.remove('hidden'); document.getElementById('backtest-results-card').classList.add('hidden'); document.getElementById('run-backtest-btn').disabled = true; document.getElementById('run-backtest-btn').textContent = 'En cours...'; const params = { start_date: document.getElementById('start_date').value, end_date: document.getElementById('end_date').value, initial_capital: document.getElementById('initial_capital').value }; await fetch('/api/backtest', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(params) }); checkBacktestStatus(); }
        async function checkBacktestStatus() { const res = await fetch('/api/backtest/status'); const data = await res.json(); const progressBar = document.getElementById('backtest-progress-bar'); const progressText = document.getElementById('backtest-progress-text'); progressBar.style.width = data.progress + '%'; progressText.textContent = Math.round(data.progress) + '%'; if (data.running) { setTimeout(checkBacktestStatus, 1000); } else { displayBacktestResults(data.results); document.getElementById('run-backtest-btn').disabled = false; document.getElementById('run-backtest-btn').textContent = 'Lancer le Backtest'; } }
        function displayBacktestResults(results) { document.getElementById('backtest-progress-card').classList.add('hidden'); document.getElementById('backtest-results-card').classList.remove('hidden'); if (results.error) { document.getElementById('backtest-summary').innerHTML = `<p class="col-span-2 text-red-400">${results.error}</p>`; return; } const summaryContainer = document.getElementById('backtest-summary'); summaryContainer.innerHTML = `<div><p class="text-sm text-gray-400">Profit Final</p><p class="text-2xl font-bold ${results.pnl > 0 ? 'text-green-400' : 'text-red-400'}">${results.pnl.toFixed(2)}</p></div><div><p class="text-sm text-gray-400">Drawdown Max</p><p class="text-2xl font-bold">${results.max_drawdown_percent.toFixed(2)}%</p></div><div><p class="text-sm text-gray-400">Taux de Réussite</p><p class="text-2xl font-bold">${results.win_rate.toFixed(2)}%</p></div><div><p class="text-sm text-gray-400">Nb. Trades</p><p class="text-2xl font-bold">${results.total_trades}</p></div>`; const ctx = document.getElementById('equity-chart').getContext('2d'); if(equityChart) equityChart.destroy(); equityChart = new Chart(ctx, { type: 'line', data: { labels: Array.from(Array(results.equity_curve.length).keys()), datasets: [{ label: 'Évolution du Capital', data: results.equity_curve, borderColor: 'rgb(75, 192, 192)', tension: 0.1, pointRadius: 0 }] }, options: { scales: { x: { display: false } } } }); }

        window.onload = () => {
            setInterval(fetchAllData, 2000);
            fetchAllData();
            loadConfigAndProfiles();
            document.getElementById('config-form').addEventListener('submit', saveConfig);
            document.getElementById('backtest-form').addEventListener('submit', runBacktest);
            document.getElementById('profile_selector').addEventListener('change', onProfileChange);
            document.getElementById('reset-profile-btn').addEventListener('click', resetProfile);
            document.getElementById('kill-switch-btn').addEventListener('click', () => { if(confirm("Êtes-vous sûr ?")) fetch('/api/kill', { method: 'POST' }); });
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
    
    def read_yaml(filepath):
        if not os.path.exists(filepath): return {}
        with open(filepath, 'r') as f: return yaml.safe_load(f)
            
    def write_yaml(filepath, data):
        with open(filepath, 'w') as f: yaml.dump(data, f, default_flow_style=False, sort_keys=False)

    if not os.path.exists('profiles.yaml'): write_yaml('profiles.yaml', DEFAULT_PROFILES)

    @app.route('/')
    def index(): return render_template_string(HTML_TEMPLATE)

    @app.route('/api/data')
    def get_all_data(): return jsonify(shared_state.get_all_data())

    @app.route('/api/config', methods=['GET', 'POST'])
    def manage_config():
        if request.method == 'POST':
            write_yaml('config.yaml', request.json)
            shared_state.update_config(request.json)
            shared_state.signal_config_changed()
            return jsonify({"status": "success"})
        return jsonify(shared_state.get_config())

    @app.route('/api/profiles', methods=['GET'])
    def get_profiles(): return jsonify(read_yaml('profiles.yaml'))

    @app.route('/api/profiles/<profile_name>', methods=['POST'])
    def update_profile(profile_name):
        profiles = read_yaml('profiles.yaml')
        profiles[profile_name] = request.json
        write_yaml('profiles.yaml', profiles)
        return jsonify({"status": "success"})

    @app.route('/api/profiles/reset/<profile_name>', methods=['POST'])
    def reset_profile(profile_name):
        if profile_name in DEFAULT_PROFILES:
            profiles = read_yaml('profiles.yaml')
            profiles[profile_name] = DEFAULT_PROFILES[profile_name]
            write_yaml('profiles.yaml', profiles)
            return jsonify({"status": "success"})
        return jsonify({"error": "Profil par défaut non trouvé"}), 404
        
    @app.route('/api/kill', methods=['POST'])
    def kill_switch(): shared_state.shutdown(); return jsonify({"message": "OK"})

    @app.route('/api/backtest', methods=['POST'])
    def handle_backtest():
        if shared_state.get_backtest_status()['running']: return jsonify({"error": "Backtest déjà en cours."}), 400
        bt = Backtester(shared_state)
        threading.Thread(target=bt.run, args=(request.json['start_date'], request.json['end_date'], request.json['initial_capital']), daemon=True).start()
        return jsonify({"status": "Backtest démarré."})

    @app.route('/api/backtest/status')
    def get_backtest_status(): return jsonify(shared_state.get_backtest_status())
    
    config = shared_state.get_config()
    if not config: config = read_yaml('config.yaml')
    app.run(host=config['api']['host'], port=config['api']['port'], debug=False)