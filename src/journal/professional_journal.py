# Fichier: src/journal/professional_journal.py
# Version: 1.0.0 (Professional-Journaling)
# Dépendances: pandas, os, logging, datetime

import pandas as pd
import os
import logging
from datetime import datetime

class ProfessionalJournal:
    """
    Gère la journalisation des trades dans un format CSV professionnel,
    avec un fichier distinct pour chaque mois.
    """
    def __init__(self, config: dict):
        self.log = logging.getLogger(self.__class__.__name__)
        self.config = config.get('professional_journal', {})
        self.enabled = self.config.get('enabled', False)
        self.file_path_template = self.config.get('file_path_template', "Journal_Trading_{month_name}_{year}.csv")

    def get_current_filepath(self) -> str:
        """Génère le chemin du fichier pour le mois et l'année en cours."""
        now = datetime.now()
        month_name = now.strftime('%B').upper()
        year = now.year
        return self.file_path_template.format(month_name=month_name, year=year)

    def record_trade(self, trade_record: dict, account_info):
        """Enregistre un trade clôturé dans le fichier CSV du mois en cours."""
        if not self.enabled:
            return

        try:
            filepath = self.get_current_filepath()
            file_exists = os.path.exists(filepath)

            capital_depart = 2000 # Valeur par défaut si le fichier n'existe pas
            if file_exists:
                try:
                    df_existing = pd.read_csv(filepath, header=2) # L'en-tête est à la 3ème ligne
                    if not df_existing.empty and '#REF!' not in str(df_existing['CAPITAL ACTUEL'].iloc[-1]):
                         # Utilise le dernier capital actuel s'il est valide
                        last_capital = pd.to_numeric(df_existing['CAPITAL ACTUEL'].dropna().iloc[-1], errors='coerce')
                        if pd.notna(last_capital):
                             capital_depart = last_capital

                except (FileNotFoundError, IndexError, pd.errors.EmptyDataError):
                     # Le fichier est peut-être vide ou mal formaté, on utilise le capital de départ
                    pass


            trade_pnl = trade_record['pnl']
            capital_actuel = capital_depart + trade_pnl
            profit_percent = (trade_pnl / capital_depart) if capital_depart > 0 else 0

            new_row = {
                'Trade': '', # Le numéro de trade sera calculé plus tard
                'Paire': trade_record['symbol'],
                'Stratégie Utilisée': trade_record['pattern_trigger'],
                'Gain / Perte': 'Gain' if trade_pnl >= 0 else 'Perte',
                '$ Profit / Perte': trade_pnl,
                '% Profit/Perte': profit_percent,
                'CAPITAL ACTUEL': capital_actuel,
                'Commentaires': f"Ticket: {trade_record['ticket']}",
                'TradingView/Image': ''
            }

            if not file_exists:
                 # Créer le fichier avec l'en-tête complet
                with open(filepath, 'w', newline='', encoding='utf-8') as f:
                    month_name = datetime.now().strftime('%B').upper()
                    f.write(f"{month_name},,,CAPITAL DE DÉPART,{account_info.balance - trade_pnl},RESULTAT,{trade_pnl},\n")
                    f.write(',,,,,,,,\n') # Ligne vide
                df_to_save = pd.DataFrame([new_row])
                df_to_save.to_csv(filepath, mode='a', header=True, index=False)

            else:
                 # Ajouter simplement la nouvelle ligne
                df_to_save = pd.DataFrame([new_row])
                df_to_save.to_csv(filepath, mode='a', header=False, index=False)
            
            self.log.info(f"Trade #{trade_record['ticket']} journalisé professionnellement dans {filepath}")

        except Exception as e:
            self.log.error(f"Erreur lors de la journalisation professionnelle du trade #{trade_record['ticket']}: {e}", exc_info=True)