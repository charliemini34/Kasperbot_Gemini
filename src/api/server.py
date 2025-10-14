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
    <title>Dashboard Bot Trading XAUUSD v5.1</title>
    <script src="https://cdn.tailwindcss.com"></script>
    <style>
        body { background-color: #111827; color: #d1d5db; }
        .card { background-color: #1f2937; border: 1px solid #374151; }
        .tab-button.active { color: #4f46e5; border-color: #4f46e5; }
        input, select { background-color: #374151; border: 1px solid #4b5563; }
        .btn-primary { background-color: #4f46e5; }
    </style>
</head>
<body class="p-6">
    <div class="max-w-7xl mx-auto">
        <header class="flex justify-between items-center mb-6">
            <h1 class="text-3xl font-bold text-white">KasperBot <span class="text-sm text-gray-400">v5.1</span></h1>
            <div id="status-indicator" class="flex items-center space-x-2">
                <div id="status-dot" class="h-4 w-4 rounded-full bg-gray-500"></div><span id="status-text" class="font-medium">Chargement...</span>
            </div>
        </header>

        <div class="lg:col-span-1 space-y-6">
            <div class="card p-5 rounded-lg"><h2 class="text-xl font-semibold text-white mb-4">Analyse des Patterns</h2><div id="patterns-container" class="space-y-2"><p class="text-gray-400">En attente...</p></div></div>
        </div>
        
        </div>
    <script>
        // ... (fonctions inchangées) ...

        function updateDashboard(data) {
            // ... (mise à jour du status, pnl, etc.)
            
            // Mise à jour de la nouvelle section des patterns
            const patternsContainer = document.getElementById('patterns-container');
            patternsContainer.innerHTML = '';
            if (Object.keys(data.status.patterns).length > 0) {
                 Object.entries(data.status.patterns).forEach(([name, d]) => {
                    const statusColor = d.status.includes('BUY') ? 'text-green-400' : (d.status.includes('SELL') ? 'text-red-400' : 'text-gray-400');
                    patternsContainer.innerHTML += `<div class="flex items-center justify-between text-sm">
                        <span class="font-medium">${name}</span>
                        <strong class="${statusColor}">${d.status}</strong>
                    </div>`;
                });
            } else {
                patternsContainer.innerHTML = '<p class="text-gray-400 text-sm">En attente d\\'analyse...</p>';
            }
            
            // ... (mise à jour des positions et logs) ...
        }
        
        // ... (Reste du script JS inchangé) ...
    </script>
</body>
</html>
"""
