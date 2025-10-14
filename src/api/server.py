# Fichier: src/api/server.py

from flask import Flask, jsonify, render_template_string, request
import yaml
import threading
import logging
import os
from src.backtest.backtester import Backtester

HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="fr">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Dashboard KasperBot v6.2</title>
    <script src="https://cdn.tailwindcss.com"></script>
    <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">
    <style>
        body { background-color: #111827; color: #d1d5db; font-family: 'Inter', sans-serif; }
        .card { background-color: #1f2937; border: 1px solid #374151; border-radius: 0.75rem; }
        .tab-button.active { color: #4f46e5; border-color: #4f46e5; }
        input, select { background-color: #374151; border: 1px solid #4b5563; border-radius: 0.375rem; padding: 0.5rem 0.75rem; }
    </style>
</head>
<body class="p-4 sm:p-6 lg:p-8">
    <div class="max-w-7xl mx-auto">
        <header class="flex justify-between items-center mb-6">
            <h1 class="text-2xl sm:text-3xl font-bold text-white">KasperBot <span class="text-sm text-gray-400">v6.2 (SMC Engine)</span></h1>
            <div id="status-indicator" class="flex items-center space-x-2">
                <div id="status-dot" class="h-4 w-4 rounded-full bg-gray-500"></div><span id="status-text" class="font-medium">Chargement...</span>
            </div>
        </header>
        <div class="mb-6"><div class="border-b border-gray-700"><nav class="-mb-px flex space-x-8">
            <button onclick="showTab('dashboard')" class="tab-button active whitespace-nowrap py-4 px-1 border-b-2 font-medium text-sm" id="tab-dashboard">Dashboard</button>
            <button onclick="showTab('backtest')" class="tab-button whitespace-nowrap py-4 px-1 border-b-2 font-medium text-sm" id="tab-backtest">Backtesting</button>
        </nav></div></div>
        <main>
            <div id="content-dashboard" class="tab-content grid grid-cols-1 lg:grid-cols-3 gap-6">
                <div class="lg:col-span-1 space-y-6">
                    <div class="card p-5"><h2 class="text-xl font-semibold text-white mb-4">État du Bot</h2><div id="bot-status-container" class="space-y-3"></div></div>
                    <div class="card p-5"><h2 class="text-xl font-semibold text-white mb-4">Analyse des Patterns</h2><div id="patterns-container" class="space-y-2"></div></div>
                </div>
                <div class="lg:col-span-2 space-y-6">
                    <div class="card p-5"><h2 class="text-xl font-semibold text-white mb-4">Positions Ouvertes</h2><div id="positions-container"></div></div>
                    <div class="card p-5"><h2 class="text-xl font-semibold text-white mb-4">Journal d'Événements</h2><div id="logs-container" class="h-96 bg-gray-900 rounded-md p-3 overflow-y-auto text-xs font-mono"></div></div>
                </div>
            </div>
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

                const patternsContainer = document.getElementById('patterns-container');
                patternsContainer.innerHTML = '';
                if (data.status.patterns && Object.keys(data.status.patterns).length > 0) {
                    Object.entries(data.status.patterns).forEach(([name, d]) => {
                        const statusColor = d.status.includes('BUY') ? 'text-green-400' : d.status.includes('SELL') ? 'text-red-400' : 'text-gray-400';
                        patternsContainer.innerHTML += `<div class="flex justify-between text-sm"><span class="font-medium">${name}</span><strong class="${statusColor}">${d.status}</strong></div>`;
                    });
                } else { patternsContainer.innerHTML = '<p class="text-gray-400 text-sm">En attente...</p>'; }

                const positionsContainer = document.getElementById('positions-container');
                positionsContainer.innerHTML = data.positions.length > 0 ? `<div class="overflow-x-auto"><table class="w-full text-left"><thead><tr class="border-b border-gray-600 text-sm"><th class="p-2">Ticket</th><th>Type</th><th>Volume</th><th>Profit</th></tr></thead><tbody class="text-sm">${data.positions.map(p => `<tr class="border-b border-gray-700"><td class="p-2">${p.ticket}</td><td class="p-2 font-bold ${p.type === 0 ? 'text-blue-400' : 'text-orange-400'}">${p.type === 0 ? 'BUY' : 'SELL'}</td><td class="p-2">${p.volume}</td><td class="p-2 font-semibold">${formatProfit(p.profit)}</td></tr>`).join('')}</tbody></table></div>` : '<p class="text-gray-400">Aucune position.</p>';

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
