# Fichier: src/journal/professional_journal.py
"""
Module pour la gestion du journal de trading professionnel.

Ce module gère la création et l'écriture des enregistrements de trades
dans un fichier CSV structuré pour une analyse ultérieure.

Version: 2.0
"""

__version__ = "2.0"

import logging
import csv
import os
from datetime import datetime
from typing import Dict, Any

logger = logging.getLogger(__name__)

class ProfessionalJournal:
    """
    Gère l'enregistrement des trades dans un fichier CSV.
    """
    
    def __init__(self, filepath: str):
        self.filepath = filepath
        
        self.csv_headers = [
            'timestamp', 'symbol', 'type', 'volume', 'entry_price', 'sl', 'tp',
            'reason', 'setup_model', 'position_id', 'status', 
            'close_price', 'close_time', 'profit'
        ]
        
        self._initialize_file()

    def _initialize_file(self):
        """
        Vérifie si le fichier journal existe et a les bons en-têtes.
        Sinon, il le crée.
        """
        try:
            file_exists = os.path.isfile(self.filepath)
            needs_header = not file_exists
            
            if file_exists:
                # Si le fichier existe, vérifier les en-têtes
                with open(self.filepath, 'r', newline='', encoding='utf-8') as f:
                    reader = csv.reader(f)
                    try:
                        existing_headers = next(reader)
                        if existing_headers != self.csv_headers:
                            logger.warning(f"Les en-têtes du journal {self.filepath} sont obsolètes. Une sauvegarde de l'ancien fichier sera créée si possible.")
                            # Idéalement, gérer une migration, mais pour l'instant on signale
                    except StopIteration:
                        needs_header = True # Fichier vide
            
            if needs_header:
                with open(self.filepath, 'w', newline='', encoding='utf-8') as f:
                    writer = csv.writer(f)
                    writer.writerow(self.csv_headers)
                    
        except IOError as e:
            logger.error(f"Erreur lors de l'initialisation du fichier journal: {e}", exc_info=True)

    def record_trade(self, trade_info: Dict[str, Any]):
        """
        Enregistre un nouveau trade (ouverture) dans le fichier CSV.

        Args:
            trade_info (Dict): Un dictionnaire contenant les informations du trade.
                               Doit correspondre aux en-têtes.
        """
        if not trade_info or 'position_id' not in trade_info:
            logger.warning("Tentative d'enregistrement d'un trade invalide.")
            return

        try:
            with open(self.filepath, 'a', newline='', encoding='utf-8') as f:
                writer = csv.writer(f)
                
                writer.writerow([
                    trade_info.get('timestamp', datetime.now().isoformat()),
                    trade_info.get('symbol', 'N/A'),
                    trade_info.get('type', 'N/A'),
                    trade_info.get('volume', 0.0),
                    trade_info.get('entry_price', 0.0),
                    trade_info.get('sl', 0.0),
                    trade_info.get('tp', 0.0),
                    trade_info.get('reason', 'N/A'),
                    trade_info.get('setup_model', 'UNKNOWN'), # Nouvelle entrée
                    trade_info.get('position_id', 0),
                    trade_info.get('status', 'OPEN'),
                    trade_info.get('close_price', None),
                    trade_info.get('close_time', None),
                    trade_info.get('profit', None)
                ])
                
        except IOError as e:
            logger.error(f"Erreur lors de l'écriture dans le fichier journal: {e}", exc_info=True)
        except Exception as e:
             logger.error(f"Erreur inattendue lors de l'enregistrement du trade: {e}", exc_info=True)

    def update_trade_close(self, position_id: Any, close_price: float, close_time: datetime, profit: float):
        """
        Met à jour un trade existant avec les informations de clôture.
        (Nécessite de lire, modifier et réécrire le CSV - peut être lent)
        
        Note: Pour des performances élevées, il serait préférable d'utiliser
        une base de données (ex: SQLite) ou de gérer les clôtures différemment.
        """
        # Implémentation simpliste pour l'instant
        logger.info(f"Mise à jour de la clôture du trade {position_id} (Non implémenté dans v2.0)")
        # TODO: Implémenter la mise à jour du CSV, ce qui est complexe.
        # Pour l'instant, le journal enregistre surtout l'ouverture.
        # L'analyse de performance devra se baser sur l'historique MT5 via l'ID.