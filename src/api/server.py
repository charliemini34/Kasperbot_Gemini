# Fichier: src/api/server.py

from flask import Flask, jsonify, render_template_string
import threading
import logging
# Le backtester n'est plus importé ici pour éviter les dépendances circulaires au démarrage

HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="fr">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Dashboard KasperBot v7.1</title>
    <script src="https://cdn.tailwindcss.com"></script>
    <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">
    <style>
        body { background-color: #111827; color: #d1d5db; font-family: 'Inter', sans-serif; }
        .card { background-color: #1f2937; border: 1px solid #374151; border-radius: 0.75rem; }
    </style>
</head>
<body class="p-4 sm:p-6 lg:p-8">
    <div class="max-w-7xl mx-auto">
        <header class="flex justify-between items-center mb-6">
            <h1 class="text-2xl sm:text-3xl font-bold text-white">KasperBot <span class="text-sm text-gray-400">v7.1 (SMC Engine)</span></h1>
            <div id="status-indicator" class="flex items-center space-x-2">
                <div id="status-dot" class="h-4 w-4 rounded-full bg-gray-500"></div><span id="status-text" class="font-medium">Chargement...</span>
            </div>
        </header>
        <main class="grid grid-cols-1 lg:grid-cols-3 gap-6">
            <div class="lg:col-span-1 space-y-6">
                <div class="card p-5"><h2 class="text-xl font-semibold text-white mb-4">État du Bot</h2><div id="bot-status-container" class="space-y-3"></div></div>
                <div class="card p-5"><h2 class="text-xl font-semibold text-white mb-4">Analyse SMC</h2><div id="patterns-container" class="space-y-2"></div></div>
            </div>
            <div class="lg:col-span-2 space-y-6">
                <div class="card p-5"><h2 class="text-xl font-semibold text-white mb-4">Positions Ouvertes</h2><div id="positions-container"></div></div>
                <div class="card p-5"><h2 class="text-xl font-semibold text-white mb-4">Journal d'Événements</h2><div id="logs-container" class="h-96 bg-gray-900 rounded-md p-3 overflow-y-auto text-xs font-mono"></div></div>
            </div>
        </main>
    </div>
    <script>
        function formatProfit(profit) { return `<span class="${parseFloat(profit) >= 0 ? 'text-green-400' : 'text-red-400'}">${parseFloat(profit).toFixed(2)}</span>`; }
        async function fetchAllData() {
            try {
                const res = await fetch('/api/data');
                const data = await res.json();
                
                const statusDot = document.getElementById('status-dot');
                statusDot.className = `h-4 w-4 rounded-full ${data.status.is_emergency ? 'bg-red-500 animate-pulse' : 'bg-green-500'}`;
                document.getElementById('status-text').textContent = data.status.status;
                document.getElementById('bot-status-container').innerHTML = `<div class="flex justify-between"><span>Status:</span> <strong>${data.status.status}</strong></div><div class="flex justify-between"><span>Message:</span> <em class="text-gray-400 text-right truncate">${data.status.message}</em></div>`;

                const patternsContainer = document.getElementById('patterns-container');
                patternsContainer.innerHTML = '';
                if (data.status.patterns && Object.keys(data.status.patterns).length > 0) {
                    Object.entries(data.status.patterns).forEach(([name, d]) => {
                        const statusColor = d.status.includes('BUY') ? 'text-green-400' : d.status.includes('SELL') ? 'text-red-400' : 'text-gray-400';
                        patternsContainer.innerHTML += `<div class="flex justify-between text-sm"><span class="font-medium">${name}</span><strong class="${statusColor}">${d.status}</strong></div>`;
                    });
                } else { patternsContainer.innerHTML = '<p class="text-gray-400 text-sm">En attente...</p>'; }

                const positionsContainer = document.getElementById('positions-container');
                positionsContainer.innerHTML = data.positions.length > 0 ? `<div class="overflow-x-auto"><table class="w-full text-left"><thead><tr class="border-b border-gray-600 text-sm"><th class="p-2">Ticket</th><th>Type</th><th>Volume</th><th>Profit</th><th>Magic</th></tr></thead><tbody class="text-sm">${data.positions.map(p => `<tr class="border-b border-gray-700"><td class="p-2">${p.ticket}</td><td class="p-2 font-bold ${p.type === 0 ? 'text-blue-400' : 'text-orange-400'}">${p.type === 0 ? 'BUY' : 'SELL'}</td><td class="p-2">${p.volume}</td><td class="p-2 font-semibold">${formatProfit(p.profit)}</td><td class="p-2">${p.magic}</td></tr>`).join('')}</tbody></table></div>` : '<p class="text-gray-400">Aucune position.</p>';

                const logsContainer = document.getElementById('logs-container');
                const newLogsHtml = data.logs.map(log => `<p>${log}</p>`).join('');
                if (logsContainer.innerHTML !== newLogsHtml) {
                    logsContainer.innerHTML = newLogsHtml;
                    logsContainer.scrollTop = logsContainer.scrollHeight;
                }
            } catch (error) { console.error("Erreur de mise à jour:", error); }
        }
        window.onload = () => { setInterval(fetchAllData, 2000); fetchAllData(); };
    </script>
</body>
</html>
"""

def start_api_server(shared_state):
    app = Flask(__name__)
    logging.getLogger('werkzeug').setLevel(logging.ERROR)

    @app.route('/')
    def index():
        return render_template_string(HTML_TEMPLATE)

    @app.route('/api/data')
    def get_all_data():
        return jsonify(shared_state.get_all_data())
    
    config = shared_state.get_config()
    host = config.get('api', {}).get('host', '127.0.0.1')
    port = config.get('api', {}).get('port', 5000)
    
    app.run(host=host, port=port, debug=False, use_reloader=False)