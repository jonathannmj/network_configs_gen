import threading
import pathlib
import os
import cv2

from ultralytics import YOLO
import easyocr
from paddleocr import PaddleOCR
import pytesseract
import matplotlib.pyplot as plt
import matplotlib.patches as patches

import sqlite3
import re
import ipaddress
import yaml
from itertools import islice, chain

import numpy as np
import shutil
from transformers import pipeline, AutoTokenizer, AutoModelForSequenceClassification
from shapely.geometry import LineString, Polygon
from shapely.ops import nearest_points
import math

from termcolor import cprint

class TopologyData:
    """Extraction des données de topologie"""
    def __init__(self):
        self.data = []
        self.reader = easyocr.Reader(['en'])
        self.status_callback = None

    def emit_status(self, message):
        if self.status_callback:
            self.status_callback(message)
        print(message)

    def process(self, imagePath, currentProjectPath, status_callback=None):
        self.status_callback = status_callback
        self.emit_status("Importing image...")
        self.import_the_image(imagePath, currentProjectPath)

        self.emit_status("Running Zones Detection...")
        self.detect_zones("zones_detection.pt")

        self.emit_status("Detecting Links...")
        self.detect_links("links_detection.pt")

        self.emit_status("Detecting Equipment Details...")
        self.equipment_detection("detect_equipment.pt")

        self.emit_status("Running OCR on Equipment Zones...")
        self.OCR_on_detected_equipments_zones()

        self.emit_status("Running OCR on Link Text Zones...")
        self.OCR_on_detected_link_text_zones()

        self.emit_status("Processing Text relations...")
        self.process_text()
        self.links_text_treatment()
        
        # Prepare enriched data for export/return
        # We inject protocol/vlan info from links_map into zoneLinkText so the UI has everything in one place
        links_map = getattr(self, "links_text_map", {})
        
        for zone_id, zone_data in self.zoneLinkText.items():
            # zone_data['interfaces'] is {link_id: name}
            # We want to add a 'protocols' dict or similar to zone_data, or enhance 'interfaces'
            # Let's add 'protocols' and 'vlans' dicts to zone_data for easy access
            zone_data['protocols'] = {}
            zone_data['vlans'] = {}
            
            for link_id in zone_data.get('interfaces', {}):
                link_meta = links_map.get(link_id, {})
                cls = link_meta.get('class')
                text = link_meta.get('text')
                
                if cls == 'protocol':
                    zone_data['protocols'][link_id] = text
                elif cls == 'vlan':
                    zone_data['vlans'][link_id] = text

        data = self.format_data_for_yaml(self.zoneLinkText)
        return data
    
    def import_the_image(self, imagePath, currentProjectPath):
        self.currentProjectPath = currentProjectPath

        # Copy the image to the current project path
        imageDirectory = pathlib.Path(currentProjectPath) / "image"
        imagePathInTheProject = shutil.copy(imagePath, imageDirectory)
        cprint(f'Image copied to: {imagePathInTheProject}', "green")
        self.imagePath = imagePathInTheProject

    def convert_width_height_to_points(self, box):
        """Convert a box (x, y, w, h) into points ((x1, y1), (x2, y2), (x3, y3), (x4, y4))"""
        (x, y, w, h) = box
        x1 = x - w/2
        y1 = y - h/2

        # x1 = xOrigin
        # y1 = yOrigin
        # x2 = x1
        # y2 = y1 + h
        # x3 = x1 + w
        # y3 = y2
        # x4 = x3
        # y4 = y1
        return ((x1, y1), (x1, y1 + h), (x1 + w, y1 + h), (x1 + w, y1))

    def AI_model_path(self, model):
        """Constitue le chemin vers le modele d'IA a utiliser"""

        # Definition of the path to the AI model
        modelsDirectory = pathlib.Path(".") / "AI_models"
        modelPath = pathlib.Path(modelsDirectory) / model
        return modelPath

    def detect_zones(self, model: str):
        """Zones detection

        It uses YOLO, and the model zones_detection_x.pt, to detect:
        - zones of equipments as the class 'equipment_zone'
        - zones of links text as the class 'linktext_zone'
        - zones of other text as the class 'extratext_zone'
        
        The result is stored in the following dictionnaries:
        - detected_equipments_zones
        - detected_linktext_zones
        - detected_extratext_zones
        """

        # Chargement de l'image
        imagePath = self.imagePath

        # Chargement du model
        modelPath = self.AI_model_path(model)
        model = YOLO(modelPath)

        # Detection des equipements
        results = model.track(imagePath)

        # Recuperation de la localisation des zones detectees et des indexes
        self.detected_equipments_zones = {}
        self.detected_linktext_zones = {}
        self.detected_extratext_zones = {}
        for result in results:
            for box in result.boxes:
                match box.cls[0]:
                    case 0:
                        # Class: 'equipment_zone'
                        id = int(box.id[0].item())
                        self.detected_equipments_zones[id] = self.extract_zones_coordinates(box)
                    case 1:
                        # Class: 'extratext_zone'
                        id = int(box.id[0].item())
                        self.detected_extratext_zones[id] = self.extract_zones_coordinates(box)
                    case 2:
                        # Class: 'linktext_zone'
                        id = int(box.id[0].item())
                        self.detected_linktext_zones[id] = self.extract_zones_coordinates(box)
                    case _:
                        continue
                
        # TODO: Remove the print()
        cprint("Equipments zones detection done", 'green')
    
    def extract_zones_coordinates(self, box):

        # Extract the coordinates of the box
        x1, y1, x2, y2 = box.xyxy[0]
        x1, x2, y1, y2 = int(x1.item()), int(x2.item()), int(y1.item()), int(y2.item())

        x, y, w, h = box.xywh[0]
        x, y, w, h = int(x.item()), int(y.item()), int(w.item()), int(h.item())

        return {'points':((x1, y1), (x2, y2)), 'box': (x, y, w, h)}   
    
    def equipment_detection(self, modelName:str):
        """Detection des equipements dans les zones detectees"""

        zones = self.detected_equipments_zones # Detected zones
        imagePath = self.imagePath  # Image path
        image = cv2.imread(imagePath)   # Reading the image with cv2

        equipments = {}
        modelPath = self.AI_model_path(modelName)

        model= YOLO(modelPath)

        for index in zones:
            equipments[index] = {}
            (x, y, w, h) = zones[index]['box']
            # regionOfInterest = image[y:y+h, x:x+w]  # Rognage de la zone
            xOrigin = x - w/2
            yOrigin = y - h/2
            regionOfInterest = image[int(yOrigin) : int(y+h), int(xOrigin) : int(x+w)]

            results = model(regionOfInterest)
            for result in results:
                if len(result.boxes.cls) > 0:
                    classes = result.names
                    classe = int(result.boxes.cls[0].item())

                    cls = classes.get(classe)
                    if cls == 'pcs':
                        cls = 'pc'

                    equipments[index] = cls
        
        self.equipments = equipments
        print(self.equipments)
        cprint("Equipments detection Done", 'green')

    def create_masks(self, image):
        """Creation des masques pour les zones detectees"""

        detectedZones = self.detected_equipments_zones

        # Masquage des zones avec equipements avant le debut de la detection des lignes (Liens)
        # Creation d'un masque
        mask = np.zeros_like(image, dtype=np.uint8)

        # Recuperation des points constituants les zones
        for zone in detectedZones.values():
            point1 = tuple(map(int, zone['points'][0]))
            point2 = tuple(map(int, zone['points'][1]))

            cv2.rectangle(mask, point1, point2, 255, -1)

        return mask

    def detect_links(self, model):
        """Detection des lignes jouants le role de lien etre deux equipements avec YOLO11  
        
        Un model de Oriented Bounding Box est utilise pour detecter les zones des liens, puis les boxes sont transformees en lignes"""

        image = self.imagePath

        # Chargement du modele
        model = YOLO(self.AI_model_path(model))

        results = model.track(image)

        self.links = {}
        for result in results:
          for obbox in result.obb:
            (x1, y1), (x2, y2), (x3, y3), (x4, y4) = obbox.xyxyxyxy[0]
            x, y, w, h, r = obbox.xywhr[0]

            ## Extraction des valeurs des tenseurs
            # Extraction des coordonnees quatre points constituants la box
            x1, x2, x3, x4 = int(x1.item()), int(x2.item()), int(x3.item()), int(x4.item())
            y1, y2, y3, y4 = int(y1.item()), int(y2.item()), int(y3.item()), int(y4.item())

            # En fonction de la hauteur et de la largeur
            x, y, w, h, r = int(x.item()), int(y.item()), int(w.item()), int(h.item()), int(r.item())

            ## Determination des deux points delimittants la droite
            points = [(x1, y1), (x2, y2), (x3, y3), (x4, y4)]
            pt1 = min(points, key=lambda p: p[1])
            pt2 = max(points, key=lambda p: p[1])

            ## Ajout des valeurs dans le dictionnaire des liens
            self.links[int(obbox.id)] = {'points': (pt1, pt2), 'box': (x, y, w, h, r)}

        cprint("Links detection Done", 'green')

        self.link_equipments()
     
    def map_links_to_midle_text(self, extractedText):
        """
        For each link, find the closest text zone and associate the text.
        Returns: dict {link_id: text}  

        Le texte est surtut considere comme l'adresse reseau ou le protocole
        """

        links = self.links
        linkText = {}

        for index in links:
            linkPoints = links[index]['points']
            linkLine = LineString(linkPoints)

            minDistance = float('inf')
            closestZoneId = None

            for zoneId, zoneData in self.detected_linktext_zones.items():
                zonePolygon = Polygon(zoneData['points'])
                
                # Skip invalid polygons
                if not zonePolygon.is_valid or zonePolygon.is_empty:
                    continue

                # Find the closest point on the link to the zone boundary
                centroid = zonePolygon.centroid
                if centroid.is_empty:
                    continue
                    
                closestPointOnZone = linkLine.interpolate(linkLine.project(centroid))
                distance = closestPointOnZone.distance(zonePolygon.boundary)
                
                if distance < minDistance:
                    minDistance = distance
                    closestZoneId = zoneId

            # Assign text if found
            if closestZoneId is not None and closestZoneId in extractedText:
                linkText[index] = extractedText[closestZoneId]['text']
            else:
                linkText[index] = None

        return linkText

    def zones_text_treatment(self):
        """Treat the text near equipments to link them to the ports they are giving informations of.

        Builds self.zoneLinkText with a safe, initialized structure per zone:
            { zone_id: {'interfaces': {link_id: iface_name}, 'ip_add': {link_id: ip}, 'hostname': str, 'notes': [...]}, ... }
        """
        equipmentsZones = getattr(self, "detectedZones", {}) or {}

        zoneLinkText = {}
        for equipmentIndex in equipmentsZones.keys():
            # initialize a safe structure for this zone
            zoneLinkText.setdefault(equipmentIndex, {
                "interfaces": {},
                "ip_address": {},
                "hostname": None,
                # "notes": []
            })

            # get the list of link ids close to this zone (safe)
            zoneLinks = []
            if self.zoneWithLinks:
                zoneLinks = [link[0] for link in self.zoneWithLinks.get(equipmentIndex)]

            # iterate OCR results for this equipment zone safely
            zoneTexts = self.extractedTextForEquipmentZones.get(equipmentIndex)
            for textIndex, entry in zoneTexts.items():
                text = entry.get("text")
                coordinates = entry.get("coordinates")
                cls = entry.get("class")
                if not text:
                    continue
                
                # Save the text depending of the class.
                # Take care of every Cases
                if cls == "hostname":
                    zoneLinkText[equipmentIndex]["hostname"] = text
                elif cls == "interface":
                    if zoneLinks and coordinates:
                        closestLink, _ = self.closest_to_the_box(coordinates, zoneLinks)
                        if closestLink is not None:
                            # ensure key exists and assign
                            zoneLinkText[equipmentIndex]["interfaces"][closestLink] = text
                        else:
                            zoneLinkText[equipmentIndex]["notes"].append({"class": cls, "text": text})
                    else:
                        zoneLinkText[equipmentIndex]["notes"].append({"class": cls, "text": text})
                elif cls == "ip_address":
                    if zoneLinks and coordinates:
                        closestLink, _ = self.closest_to_the_box(coordinates, zoneLinks)
                        # if closestLink is not None:
                        zoneLinkText[equipmentIndex]["ip_address"][closestLink] = text
                #         else:
                #             zoneLinkText[equipmentIndex]["notes"].append({"class": cls, "text": text})
                #     else:
                #         zoneLinkText[equipmentIndex]["notes"].append({"class": cls, "text": text})
                # else:
                #     zoneLinkText[equipmentIndex]["notes"].append({"class": cls, "text": text})

        cprint("Zone Link Text:", "blue")
        print(zoneLinkText)

        self.zoneLinkText = zoneLinkText

    def links_text_treatment(self):
        """Process texts found on links and associate them with link endpoints and equipment interfaces.

        Produces:
            self.links_text_map: dict where key = link_id and value =
                {
                    "text": str or None,
                    "class": str or None,
                    "endpoints": tuple(zoneA, zoneB) or ();
                    "endpoint_interfaces": { zone_id: interface_name, ... }
                }
        """
        links_text = self.textOnLinks
        link_map = {}

        for link_id, info in links_text.items():
            text = info.get("text")
            cls = info.get("class")

            # Try to get endpoints from linkedEquipments (link_id -> (zoneA, zoneB))
            endpoints = None
            if self.linkedEquipments:
                endpoints = self.linkedEquipments.get(link_id)

            # Fallback: search zoneWithLinks for zones that reference this link
            if not endpoints and hasattr(self, "zoneWithLinks"):
                zones = []
                for zone, links in getattr(self, "zoneWithLinks", {}).items():
                    for item in links:
                        # item usually (linkId, point)
                        try:
                            if item[0] == link_id:
                                zones.append(zone)
                                break
                        except Exception:
                            continue
                if zones:
                    endpoints = tuple(zones)

            if endpoints is None:
                endpoints = ()

            # Find interfaces on each endpoint associated with this link (if equipmentInterfaces exists)
            endpoint_interfaces = {}
            try:
                eq_ifaces = getattr(self, "equipmentInterfaces", {}) or {}
                for zone in endpoints:
                    if zone in eq_ifaces:
                        for entry in eq_ifaces[zone].get("interfaces", []):
                            # expected entry format: (closestLink, text, kind) from map_links_to_port_text
                            if isinstance(entry, (list, tuple)) and len(entry) >= 2:
                                if entry[0] == link_id:
                                    endpoint_interfaces[zone] = entry[1]
                                    break
                            # also handle dict-style entries if present
                            if isinstance(entry, dict):
                                if entry.get("link_id") == link_id or entry.get("link") == link_id:
                                    endpoint_interfaces[zone] = entry.get("name") or entry.get("interface") or entry.get("text")
                                    break
            except Exception:
                # be resilient to unexpected formats
                pass

            link_map[link_id] = {
                "text": text,
                "class": cls,
                "endpoints": endpoints,
                "endpoint_interfaces": endpoint_interfaces
            }

        self.links_text_map = link_map

    def text_classification(self, text):
        """Classification du texte detecte entre:
        - hostname
        - ip_address
        - protocol
        - vlan
        - interface"""

        model_path = self.AI_model_path("bert-finetuned") # ./AI_models/bert-finetuned

        # Load tokenizer and model from local folder
        tokenizer = AutoTokenizer.from_pretrained(model_path, local_files_only=True)
        model = AutoModelForSequenceClassification.from_pretrained(model_path, local_files_only=True)

        # Add mapping from label ids to human-readable tags
        label_names = {
            0: "hostname",
            1: "interface",
            2: "ip_address",
            3: "protocol",
            4: "vlan"
        }
        model.config.id2label = {i: name for i, name in label_names.items()}
        model.config.label2id = {name: i for i, name in label_names.items()}

        # Create pipeline using the loaded objects
        classifier = pipeline("text-classification", model=model, tokenizer=tokenizer)
        result = classifier(text)

        return result

    def filter_interfaces(self):
        """Filtrage du texte detecte pret des ports pour ne garder que le texte qui correspond a la sytax des interfaces et des adresses IP"""
        # TODO: Usage to be defined
        zoneText = self.extractedTextForEquipmentZones
        normalise = True
        filteredText = []
        for text in zoneText:
            normalText = self.normalise_interfaces_names(text) if normalise else text
            if self.is_valid_interface(normalText):
                filteredText.append(normalText)
        return filteredText

    def filter_IP_addresses(self):
        """Filtrage du texte detecte pour ne garder que les adresses IP"""
        zoneText = self.extractedTextForEquipmentZones
        IPText = {}
        for text in zoneText:
            if self.is_valid_ip(text):
                IPText[text] = {'text': text, 'coordinates': zoneText[text]['coordinates']}
        return IPText

    def normalise_interfaces_names(self, text):
        """
        Convertit les abréviations en noms complets d'interfaces réseau.
        Supporte: f0/0, gi0/0, s0/0, eth0, etc.
        Gère également les erreurs OCR où '0' est lu comme 'o' ou 'O'.
        """
        substitutions = {
            r'(?i)^(?:GigabitEthernet|Gig|Gi|g)([\doO]+(?:/[\doO]+){1,2})$': r'GigabitEthernet\1',
            r'(?i)^(?:FastEthernet|FastEth|Fa|fo|f)([\doO]+(?:/[\doO]+){1,2})$': r'FastEthernet\1',
            r'(?i)^(?:TenGigabitEthernet|TenGig|Te)([\doO]+(?:/[\doO]+){1,2})$': r'TenGigabitEthernet\1',
            r'(?i)^(?:Ethernet|Eth|e)([\doO]+(?:/[\doO]+){0,2})$': r'Ethernet\1',
            r'(?i)^(?:Serial|Se|s)([\doO]+(?:/[\doO]+){1,3})$': r'Serial\1',
            r'(?i)^(?:Loopback|Lo)([\doO]+)$': r'Loopback\1',
            r'(?i)^(?:Vlan|Vl|v)([\doO]+)$': r'Vlan\1',
            r'(?i)^(?:Port-channel|Po)([\doO]+)$': r'Port-channel\1',
        }

        for pattern, replacement in substitutions.items():
            match = re.match(pattern, text)
            if match:
                # Extract the numeric part (Always group 1)
                numeric_part = match.group(1)
                # Correction OCR : remplacer 'o' et 'O' par '0' uniquement dans la partie numérique
                cleaned_numeric = numeric_part.replace('o', '0').replace('O', '0')
                
                # Extract the standard prefix from the replacement string
                # e.g. r'GigabitEthernet\1' -> 'GigabitEthernet'
                prefix = replacement.replace(r'\1', '')
                
                return prefix + cleaned_numeric
        return text  # Retourne tel quel si aucune correspondance

    def is_interface(self, text):
        """
        Vérifie si le nom correspond à une interface réseau Cisco typique.
        """
        normalise = True
        normalText = self.normalise_interfaces_names(text) if normalise else text # Normalisation of the interface text before testing if it is a valid interface name.
        validPatterns = [
            r'^GigabitEthernet\s?\d+(/\d+){1,2}$',
            r'^FastEthernet\s?\d+(/\d+){1,2}$',
            r'^Serial\s?\d+(/\d+){1,3}$',
            r'^Loopback\d+$',
            r'^Ethernet\s?\d+(/\d+){0,2}$',
            r'^Port-channel\s?\d+$',
            r'^TenGigabitEthernet\s?\d+(/\d+){1,2}$'
        ]
        
        return any(re.match(pattern, normalText) for pattern in validPatterns)

    def is_hostname(self, text):
        """Checks if the text corresponds to a valid hostname."""

        if text.isdigit():
            return False

        # Alphanumeric, hyphens, underscores, dots (for FQDN). Should usually start with a letter/digit.
        pattern = r'^[a-zA-Z0-9]([a-zA-Z0-9\-\_]{0,61}[a-zA-Z0-9])?(\.[a-zA-Z0-9]([a-zA-Z0-9\-\_]{0,61}[a-zA-Z0-9])?)*$'
        return bool(re.match(pattern, text))

    def is_protocol(self, text):
        """Checks if the text is a known network protocol."""
        protocols = [
            'OSPF', 'BGP', 'EIGRP', 'RIP', 'ISIS', 'HSRP', 'VRRP', 'GLBP', 
            'STP', 'RSTP', 'MSTP', 'LACP', 'PAgP', 'DHCP', 'DNS', 'NTP', 
            'SNMP', 'SSH', 'Telnet', 'HTTP', 'HTTPS', 'FTP', 'TFTP', 'ICMP', 
            'TCP', 'UDP', 'GRE', 'IPsec', 'MPLS', 'LDP'
        ]
        return text.upper() in protocols

    def is_vlan(self, text):
        """Checks if the text looks like a VLAN identifier."""
        # e.g., "10", "VLAN 10", "vlan100"
        pattern = r'^(VLAN\s?)?\d{1,4}$'
        return bool(re.match(pattern, text, re.IGNORECASE))

    def is_complete_ip(self, text):
        """Checks if the text is a complete IPv4 address (with optional CIDR)."""
        # 4 octets
        pattern = r'^((25[0-5]|2[0-4][0-9]|[01]?[0-9][0-9]?)\.){3}(25[0-5]|2[0-4][0-9]|[01]?[0-9][0-9]?)(/(3[0-2]|[12]?[0-9]))?$'
        return bool(re.match(pattern, text))

    def is_incomplete_ip(self, text):
        """Checks if the text is an incomplete IPv4 address segment."""
        # Matches patterns like .1, .1.1, 1.2, .1/24 etc.
        # Cases:
        # 1. Starts with dot: .1, .1.2, .1.2.3, .1/24
        # 2. X.Y (at least one dot) but not complete 4 octets? 
        #    Note: 1.2 could be a version number. But in network context we might accept it.
        #    Let's restrict "No starting dot" to at least digits.digits
        
        # Pattern 1: Starts with dot, followed by octet, optionally more dot-octets, optional CIDR
        p1 = r'^\.(\d{1,3})(\.\d{1,3})*(/(3[0-2]|[12]?[0-9]))?$'
        
        # Pattern 2: Digits.Digits... (partial)
        p2 = r'^(\d{1,3}\.)+\d{1,3}(/(3[0-2]|[12]?[0-9]))?$'

        # Pattern 3: Simple number (1-3 digits) - e.g. "2", "10"
        # This is for when OCR misses the dot or it's just the host part
        p3 = r'^\d{1,3}$'
        
        if re.match(p1, text) or re.match(p3, text):
            return True
            
        if re.match(p2, text):
            # Check if it is NOT a complete IP (4 octets)
            if text.count('.') == 3:
                 return False
            return True
            
        return False
    
    def is_ip(self, text):
        """Verifie si le texte correspond a une adresse IP"""

        patterns = [
            r'\b(?:\d{1,3}\.){1,3}\d{1,3}\b',
            r'\b(?:\.\d{1,3}\.){1,2}\d{1,3}\b',
            r'\b(?:\.\d{1,3})'
        ]

        validIPPatterns = re.compile('|'.join(patterns))

        return validIPPatterns.fullmatch(text) 

    def is_ip_with_mask(self, text):
        """Determine si le texte est l'adresse du reseau"""
        
        try:
            ipaddress.IPv4Network(text, strict=False)
            return True
        except ValueError:
            return False

    def classify_text(self, texts):
        """Classify the text"""
        if isinstance(texts, list):
            result = []
            for index, text in enumerate(texts):
                if self.is_interface(text): result.append((index, 'interface'))
                elif self.is_hostname(text): result.append((index, 'hostname'))
                elif self.is_protocol(text): result.append((index, 'protocol'))
                elif self.is_incomplete_ip(text): result.append((index, 'incomplete_ip'))
                elif self.is_vlan(text): result.append((index, 'vlan'))
                elif self.is_ip(text): result.append((index, 'ip'))
                elif self.is_ip_with_mask(text): result.append((index, 'ip'))
                else: result.append((index, 'other'))
            
            return result
        
        else:
            text = texts
            if self.is_hostname(text): return "hostname"
            elif self.is_protocol(text): return "protocol"
            elif self.is_incomplete_ip(text): return "incomplete_ip"
            elif self.is_vlan(text): return "vlan"
            elif self.is_ip(text): return "ip"
            elif self.is_ip_with_mask(text): return "ip"
            elif self.is_interface(text): return "interface"
            else: return "other"

    def map_links_to_port_text(self):
        """Lie chaque extremite d'un lien avec le texte qui s'y rapporte"""

        self.links
        linkedEquipments = self.linkedEquipments
        texts = self.extractedTextForEquipmentZones
        equipmentZone = self.detected_equipments_zones
        
        # Liste des liens qui aboutissent a une zone
        zoneWithLinks = {}
        for zone in equipmentZone:
            links = [] # Liste pour le stockage des liens avant de les sauvegarder dans le dictionnaire avec la zone appropriee
            for link in linkedEquipments:
                if zone in linkedEquipments[link]:
                    links.append(link)
            zoneWithLinks[zone] = links

        equipmentInterfaces = {}

        for zone in zoneWithLinks:
            equipmentInterfaces[zone] = {'interfaces': []}
            interfacesList = equipmentInterfaces[zone]['interfaces']
            for text, textCoordinates in texts[zone].values():
                if self.is_valid_interface(text): # Si le texte est le nom d'une interface
                    _, closestLink = self.find_nearest_link(zone, zoneWithLinks, self.links, textCoordinates)
                    interfacesList.append((closestLink, text, 'int'))
                elif self.is_valid_ip(text): # Sinon si le texte est une adresse IP
                    _, closestLink = self.find_nearest_link(zone, zoneWithLinks, self.links, textCoordinates)
                    interfacesList.append((closestLink, text, 'ip'))
                else: # Sinon: Considerer le texte comme le hostname de l'equipement
                    equipmentInterfaces[zone]['hostname'] = text

        self.equipmentInterfaces = equipmentInterfaces
                
    def find_nearest_link(self, zone, zoneWithLinks, links, textCoordinates):
        minDistance = float('inf')
        closestLink = None
        x, y, w, h = textCoordinates
        zoneCenter = [x + w // 2, y + h // 2]
        for link in zoneWithLinks[zone]:
            (x1, y1), (x2, y2) = links[link]['points']
            distance1 = math.sqrt((x1 - zoneCenter[0])**2 + (y1 - zoneCenter[1])**2)
            distance2 = math.sqrt((x2 - zoneCenter[0])**2 + (y2 - zoneCenter[1])**2)
            distance = min(distance1, distance2)
            if distance < minDistance:
                minDistance = distance
                closestLink = link

        return [minDistance, closestLink]

    def link_equipments(self):
        """Lie les equipements a l'aide des liens qui ont ete detectes"""

        detectedLinks = self.links
        detectedZones = self.detected_equipments_zones

        zoneWithLinks = {}
        for index in detectedZones:
            zoneWithLinks[index] = []
            zone = detectedZones[index]
            zone_points = self.convert_width_height_to_points(zone['box'])
            closestLinks = self.closest_to_the_box(zone_points, detectedLinks.keys(), True)
            # (x, y, w, h) = zone['box']
            # zoneCenter = (x + w // 2, y + h // 2)
            # halfWay = max(w // 2, h // 2) + 4
            # links = []
            # for linkIndex in detectedLinks:
            #     link = detectedLinks[linkIndex]
            #     (x1, y1), (x2, y2) = link['points']
            #     # Determination de la distance entre le point et le centre de la zone
            #     if 0 < math.sqrt((x1 - zoneCenter[0])**2 + (y1 - zoneCenter[1])**2) < halfWay:
            #         point = (x1, y1)
            #         links.append((linkIndex, point))
            #     elif 0 < math.sqrt((x2 - zoneCenter[0])**2 + (y2 - zoneCenter[1])**2) < halfWay:
            #         point = (x2, y2)
            #         links.append((linkIndex, point))
            
            if closestLinks:
                zoneWithLinks[index] = closestLinks
        
        self.zoneWithLinks = zoneWithLinks

        # linked equipments
        linked = {}
        zones = [zone for zone in zoneWithLinks.keys()]
        for i in range(len(zoneWithLinks.keys())-1):
            zone1 = zoneWithLinks[zones[i]]
            zone2 = zoneWithLinks[zones[i+1]]

            # Check if the zones are linked
            link = self.are_zones_linked(zone1, zone2)
            if link:
                linked[link] = (i, i+1)

        cprint(f'LINKED', 'green')
        cprint(linked, 'green')
        cprint(f'Detected zones\n{detectedZones}', 'yellow')
        cprint(f'Detected links\n{detectedLinks}', 'blue')
        cprint(f'Zone with links\n{zoneWithLinks}', 'green')
        cprint('----------------------------\n', 'green')

        self.linkedEquipments = linked

    def are_zones_linked(self, zone1, zone2):
        """Detect linked zones in the image"""
        for link1 in zone1:
            for link2 in zone2:
                if link1[0] == link2[0]:
                    return link1
            
    def closest_to_the_box(self, box, linksList, multiple = False):
        """Determines the shortest path between the text box and the links

        It takes as inputs:
        - box: (x, y, w, h)
        - links: {link_id: {'points': ((x1, y1), (x2, y2)), 'box': (x, y, w, h, r)}}

        It returns:
        - closestLink: the id of the closest link
        - distance: the distance between the box and the closest link
        """
        links = self.links
        ((x1, y1), (x2, y2), (x3, y3), (x4, y4)) = box
        box = [(x1, y1), (x2, y2), (x3, y3), (x4, y4)] # Coordinates of the points of the box
        rectangle = Polygon(box) # Rectangle representing the box

        # Initialisation of the minimum distance and the closest link
        minDistance = float('inf')
        closestLink = None

        if not multiple:
            for linkIndex in linksList:
                link = links[linkIndex]['points']
                linkLine = LineString(link) # Line representing the link

                # use nearest_points
                p_rect, p_link = nearest_points(rectangle.boundary, linkLine)
                distance = p_rect.distance(p_link)
                if distance < minDistance:
                    minDistance = distance
                    closestLink = linkIndex

            if closestLink is None:
                return None, None
            else:
                return closestLink, minDistance
        else:
            closests = []
            for index in linksList:
                link = links[index]['points']
                linkLine = LineString(link) # Line representing the link

                # use nearest_points
                p_rect, p_link = nearest_points(rectangle.boundary, linkLine)
                distance = p_rect.distance(p_link)
                if distance < 2:
                    closests.append((index, distance))
            return closests

    def create_links(self, linked):
        """Create links between zones and lines"""
        pass

    def OCR_on_detected_equipments_zones(self):
        """Perform OCR on the detected equipments on the image and save the results in the database"""

        # Chargement de l'image
        image = cv2.imread(self.imagePath)

        # OCR on each detected zone
        # Or Targeted OCR
        self.extractedTextForEquipmentZones = self.targeted_OCR(image, self.detected_equipments_zones)

        # classify text        
        # 1 Make a list of all the text
        all_text = []
        for zone in self.extractedTextForEquipmentZones:
            for text in self.extractedTextForEquipmentZones[zone]:
                all_text.append(self.extractedTextForEquipmentZones[zone][text]['text'])

        print(f'all_text\n{all_text}')

        #2 Classify the list text
        classes = self.classify_text(all_text)
        print(f'classes\n{classes}')

        #3 Put the classified text in the extractedTextForEquipmentZones
        index = 0
        for zone in self.extractedTextForEquipmentZones:
            for text in self.extractedTextForEquipmentZones[zone]:
                self.extractedTextForEquipmentZones[zone][text]['class'] = classes[index][1]
                index += 1

        # cprint("Text from equipments zones", 'blue')
        # print(self.extractedTextForEquipmentZones)
        cprint("----------------------------\n", 'blue')

    def OCR_on_detected_link_text_zones(self):
        """OCR on detected zones of text"""

        imagePath = self.imagePath
        detectedTextZones = self.detected_linktext_zones

        image = cv2.imread(imagePath)
        gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)

        # Binarisation
        _, binary = cv2.threshold(gray, 200, 255, cv2.THRESH_BINARY_INV)

        # OCR on each detected zone
        # Or Targeted OCR        
        extractedTextForLinks = self.targeted_OCR(image, detectedTextZones)
        
        # Link texts to related links
        self.textOnLinks = self.link_text_to_links(extractedTextForLinks)

        # Print the extracted links data
        cprint("Links data", 'blue')
        print(self.textOnLinks)
        # print(detectedTextZones)
        cprint("-----------------------------------")

    def targeted_OCR(self, image, detected_zones):
        """Use OCR on detected zones to extract the text in it
        """
        length = len(detected_zones)
        step = length // 3

        # Initialize the dictionnaries where will be saved the results
        result1 = {}
        result2 = {}
        result3 = {}
        
        # Instantiate the reader once
        # Instantiate separate PaddleOCR instances for each thread to ensure thread safety
        # Also disable MKLDNN to avoid PIR conversion errors
        reader_1 = PaddleOCR(use_angle_cls=True, lang='en', enable_mkldnn=False, return_word_box=True)
        reader_2 = PaddleOCR(use_angle_cls=True, lang='en', enable_mkldnn=False, return_word_box=True)
        reader_3 = PaddleOCR(use_angle_cls=True, lang='en', enable_mkldnn=False, return_word_box=True)

        # Create a thread to do the classification on each part of the list
        thread_1 = threading.Thread(target=self.paddleOCR, args=(image, dict(islice(detected_zones.items(), 0, step)), result1, reader_1))
        thread_2 = threading.Thread(target=self.paddleOCR, args=(image, dict(islice(detected_zones.items(), step, step*2)), result2, reader_2))
        thread_3 = threading.Thread(target=self.paddleOCR, args=(image, dict(islice(detected_zones.items(), step*2, length)), result3, reader_3))

        # Start the threads
        thread_1.start()
        thread_2.start()
        thread_3.start()
        
        # Wait for all the threads to finish their work before continuing
        thread_1.join()
        thread_2.join()
        thread_3.join()

        extractedText = dict(chain(result1.items(), result2.items(), result3.items()))
        cprint("Extracted text", 'red')
        print(extractedText)
        cprint("@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@", 'red')
            
        return extractedText

    def easyOCR(self, image, zones, result, reader_instance):
        for index in zones:
            result[index] = {}
            (x, y, w, h) = zones[index]['box']
            xOrigin = x - w/2
            yOrigin = y - h/2
            
            # Extract region of interest
            regionOfInterest = image[int(yOrigin) : int(y+h), int(xOrigin) : int(x+w)]

            if regionOfInterest.size == 0:
                continue

            # --- Preprocessing for better OCR accuracy ---
            # 1. Convert to grayscale
            gray = cv2.cvtColor(regionOfInterest, cv2.COLOR_BGR2GRAY)

            # 2. Upscale the image (3x helps with small text)
            scale_factor = 3
            upscaled = cv2.resize(gray, None, fx=scale_factor, fy=scale_factor, interpolation=cv2.INTER_CUBIC)

            # 3. Add padding (white border) - critical for text touching edges
            # Add 10px white border (assuming grayscale 255 is white)
            padding = 10
            padded = cv2.copyMakeBorder(upscaled, padding, padding, padding, padding, cv2.BORDER_CONSTANT, value=255)

            # 4. Optional: Denoising / Thresholding (can sometimes help, but easyocr handles some internally)
            threshold = cv2.threshold(padded, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)[1]

            # 5. Optional: Noise Reduction (can help with very noisy images)
            denoised = cv2.fastNlMeansDenoising(threshold, None, h=10, templateWindowSize=7, searchWindowSize=21)
            
            # 6. Optional: Image Enhancement (can help with low contrast)
            enhanced = cv2.equalizeHist(denoised)

            # 7 Increase contrast
            enhanced = cv2.convertScaleAbs(enhanced, alpha=2, beta=0)
            
            # Debug: save processed image (optional)
            # cv2.imwrite(f"debug_ocr_{index}.png", padded)
            
            # Read the text from the zone with 'easyocr'
            ocrResult = reader_instance.readtext(
                enhanced,
                decoder='beamsearch',
                contrast_ths=0.05,
                adjust_contrast=0.7,
                text_threshold=0.5, # Lowered from 0.6
                low_text=0.3,       # Lowered from 0.4
                mag_ratio=1.0,       # We already upscaled
                link_threshold=0.5
            )

            # cprint("OCR result", 'green')
            # print(ocrResult)
            # cprint("----------------------\n", 'green')

            counter = 0
            for (bbox, text, probability) in ocrResult:
                if probability >= 0.5:   # Lowered threshold slightly to catch more potential matches
                    result[index][counter] = {'text': text, 'coordinates': bbox}
                    counter += 1

    def paddleOCR(self, image, zones, result, reader_instance):
        """Use PaddleOCR on detected zones to extract the text in it using the same logic as in the OCR function"""

        for index in zones:
            result[index] = {}
            (x, y, w, h) = zones[index]['box']
            xOrigin = x - w/2
            yOrigin = y - h/2
            
            # Extract region of interest
            regionOfInterest = image[int(yOrigin) : int(y+h), int(xOrigin) : int(x+w)]

            if regionOfInterest.size == 0:
                continue

            # --- Preprocessing for better OCR accuracy ---
            # 1. Convert to grayscale
            gray = cv2.cvtColor(regionOfInterest, cv2.COLOR_BGR2GRAY)

            # 2. Upscale the image (3x helps with small text)
            scale_factor = 3
            upscaled = cv2.resize(gray, None, fx=scale_factor, fy=scale_factor, interpolation=cv2.INTER_CUBIC)

            # 3. Add padding (white border) - critical for text touching edges
            # Add 10px white border (assuming grayscale 255 is white)
            padding = 10
            padded = cv2.copyMakeBorder(upscaled, padding, padding, padding, padding, cv2.BORDER_CONSTANT, value=255)

            # 4. Optional: Denoising / Thresholding (can sometimes help, but easyocr handles some internally)
            threshold = cv2.threshold(padded, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)[1]

            # 5. Optional: Noise Reduction (can help with very noisy images)
            denoised = cv2.fastNlMeansDenoising(threshold, None, h=10, templateWindowSize=7, searchWindowSize=21)
            
            # 6. Optional: Image Enhancement (can help with low contrast)
            enhanced = cv2.equalizeHist(denoised)

            # 7 Increase contrast
            enhanced = cv2.convertScaleAbs(enhanced, alpha=2, beta=0)
            
            # Debug: save processed image (optional)
            # cv2.imwrite(f"debug_ocr_{index}.png", padded)
            
            # Read the text from the zone with 'easyocr'
            ocrResult = reader_instance.predict(regionOfInterest)

            cprint("OCR result", 'green')
            for idx in range(len(ocrResult)):
                res = ocrResult[idx]
                print(res.get('rec_texts'))
                print(res.get('rec_scores'))
                print(res.get('rec_polys'))

                counter = 0
                for idx, text in enumerate(res.get('rec_texts')):
                    if res.get('rec_scores')[idx] >= 0.5:   # Lowered threshold slightly to catch more potential matches
                        result[index][counter] = {'text': text, 'coordinates': res.get('rec_polys')[idx].tolist()}
                        counter += 1
            cprint("----------------------\n", 'green')

    def closest_to_the_link(self, link):
        """Determine la distance la plus faible entre le lien et la zone de texte

        regionsPoints = [(x1, y1), (x2, y2)]  
        link = [(x1, y1), (x2, y2)]

        En sortie il renvoie les coordonnees du point le plus proche du lien et la distance entre ce point et le lien.
        """

        textZones = self.detected_linktext_zones
        zoneDistancePair = []
        for index in textZones:
            # Convert width and height box to four points box
            text_points = self.convert_width_height_to_points(textZones[index]['box'])

            # Text zone rectangle
            rectangle = Polygon(text_points)

            # Link line
            linkLine = LineString(link)

            # Skip invalid rectangles
            if not rectangle.is_valid or rectangle.is_empty:
                continue

            # Determine the closest points between the link and the rectangle boundary
            pointOnLink, p_on_rectangle = nearest_points(linkLine, rectangle.boundary)
            distance = pointOnLink.distance(p_on_rectangle)
            zoneDistancePair.append((index, distance))
        
        # Guard against empty list to avoid ValueError from min()
        if not zoneDistancePair:
            return None, None
        else:
            # Return the index of the text zone with the minimum distance and the minimum distance
            return min(zoneDistancePair, key=lambda x: x[1])
        
    def link_text_to_links(self, text):
        """
        Links text to the corresponding links
        """
        links = self.links
        textOnLinks = {}
        for link in links.keys():
            closestTextZone, _ = self.closest_to_the_link(links[link]['points'])
            joined = None
            if closestTextZone is not None and closestTextZone in text:
                zone_entries = text[closestTextZone]
                # collect all OCR pieces in the zone and join them
                pieces = [d.get('text', '').strip() for d in zone_entries.values() if d.get('text', '').strip()]
                if pieces:
                    joined = " ".join(pieces)

                    # classify the joined text
                    cls = self.classify_text(joined)

            textOnLinks[link] = {'zone': closestTextZone, 'text': joined, 'class': cls}

        return textOnLinks
    
    def process_text(self):
        """
        Process the extracted text and links data to create a topology data structure
        """
        extracted_text = self.extractedTextForEquipmentZones
        cprint("Extracted text", 'green')
        print(extracted_text)
        cprint("----------------------\n", 'green')
        links_text = self.textOnLinks
        
        filtered_text = {}
        for zone_index in extracted_text.keys():
            zoneLinks = [link[0] for link in self.zoneWithLinks.get(zone_index)]
            device = self.equipments.get(zone_index, 'unknown')
            filtered_text[zone_index] = {
                'device': device,
                'hostname': None,
                'interfaces': {},
                'ip_addresses': {},
            }
            for text_index, text_entry in extracted_text[zone_index].items():
                text = text_entry.get('text')
                cls = text_entry.get('class')
                
                match cls:
                    case 'hostname':
                        filtered_text[zone_index]['hostname'] = text
                    case 'interface':
                        closest_link, _ = self.closest_to_the_box(text_entry.get('coordinates'), zoneLinks)
                        if closest_link is not None:
                             filtered_text[zone_index]['interfaces'][closest_link] = text
                    case 'ip':
                        if device != 'pc' and device != 'server':
                            closest_link, _ = self.closest_to_the_box(text_entry.get('coordinates'), zoneLinks)
                            if closest_link is not None:
                                filtered_text[zone_index]['ip_addresses'][closest_link] = text
                        else:
                            filtered_text[zone_index]['ip_address'] = text
                    case 'incomplete_ip':
                        cprint(f"\nINCOMPLETE IP: {text}", 'yellow')
                        closest_link, _ = self.closest_to_the_box(text_entry.get('coordinates'), zoneLinks)

                        if closest_link is None:
                            continue

                        # Get the ip from the closest link
                        # Add safer get for links_text
                        link_data = links_text.get(closest_link, {})
                        network_ip = link_data.get('text') if link_data.get('class') == 'ip' else None

                        cprint(f"NETWORK IP: {network_ip}", 'yellow')
                        cprint(f"CLOSEST LINK: {closest_link}", 'yellow')
                        
                        if network_ip:
                            # Complete the ip address based on the network_ip
                            complete_ip = self.complete_the_ip_address(text, network_ip)
                            cprint(f'COMPLETE IP: {complete_ip}', 'yellow')
                        else:
                            complete_ip = text

                        if device != 'pc' and device != 'server':
                            filtered_text[zone_index]['ip_addresses'][closest_link] = complete_ip
                        else:
                            filtered_text[zone_index]['ip_address'] = complete_ip
                    case _:
                        continue  # unknown class
            
        self.zoneLinkText = filtered_text

    def complete_the_ip_address(self, incomplete_ip, network_id):
        """
        Completes an incomplete IP address to a full IP address
        """
        # Network address gathering
        try:
            network = ipaddress.IPv4Network(network_id)
        except Exception as e:
            cprint(f"Error while trying to complete the ip address: {e}", 'red')
            return None

        network_add = network.network_address
        netmask = network.netmask
        prefix_len = network.prefixlen

        cprint(f"NETWORK ADDRESS: {network_add}", 'yellow')
        cprint(f"NETMASK: {netmask}", 'yellow')
        cprint(f"PREFIX LENGTH: {prefix_len}", 'yellow')

        # Remove CIDR mask from incomplete_ip if present
        if '/' in incomplete_ip:
            incomplete_ip = str(incomplete_ip).split('/')[0]
        
        incomplete_ip_list = str(incomplete_ip).split('.')
        incomplete_ip_list = [el for el in incomplete_ip_list if el != '']

        cprint(f"INCOMPLETE IP LIST: {incomplete_ip_list}", 'yellow')

        # Determine the host part
        network_add_list = str(network_add).split('.')
        netmask_list = str(netmask).split('.')

        cprint(f"NETWORK ADDRESS LIST: {network_add_list}", 'yellow')
        cprint(f"NETMASK LIST: {netmask_list}", 'yellow')
        
        for i, mask in enumerate(netmask_list):
            if mask != '255':
                step = 256 - int(mask) # Determine possible host values range
                hostbytes = len(incomplete_ip_list)
                print(hostbytes)
                match i:
                    # Return complete address
                    case 0:
                        return None
                    case 1:
                        if hostbytes >= 3: 
                            if int(incomplete_ip_list[-3]) in range(int(network_add_list[1]) + 1, step - 1):
                                return f"{network_add_list[0]}.{incomplete_ip_list[-3]}.{incomplete_ip_list[-2]}.{incomplete_ip_list[-1]}/{str(prefix_len)}"
                            else: return None
                        elif hostbytes == 2:
                            if int(incomplete_ip_list[-2]) in range(int(network_add_list[1]) + 1, step - 1):
                                return f"{network_add_list[0]}.{network_add_list[1]}.{incomplete_ip_list[-2]}.{incomplete_ip_list[-1]}/{str(prefix_len)}"
                            else: return None
                        elif hostbytes == 1:
                            if int(incomplete_ip_list[-1]) in range(int(network_add_list[1]) + 1, step - 1):
                                return f"{network_add_list[0]}.{network_add_list[1]}.{network_add_list[2]}.{incomplete_ip_list[-1]}/{str(prefix_len)}"
                            else: return None
                    case 2:
                        if hostbytes >= 2:
                            if int(incomplete_ip_list[-2]) in range(int(network_add_list[2]) + 1, step - 1):
                                return f"{network_add_list[0]}.{network_add_list[1]}.{incomplete_ip_list[-2]}.{incomplete_ip_list[-1]}/{str(prefix_len)}"
                            else: return None
                        elif hostbytes == 1:
                            if int(incomplete_ip_list[-1]) in range(int(network_add_list[2]) + 1, step - 1):
                                return f"{network_add_list[0]}.{network_add_list[1]}.{network_add_list[2]}.{incomplete_ip_list[-1]}/{str(prefix_len)}"
                            else: return None
                    case 3:
                        if hostbytes == 1:
                            if int(incomplete_ip_list[-1]) in range(int(network_add_list[3]) + 1, step - 1):
                                return f"{network_add_list[0]}.{network_add_list[1]}.{network_add_list[2]}.{incomplete_ip_list[-1]}/{str(prefix_len)}"
                            else: return None

    def is_data_extracted(self, currentProjectPath):
        privateDB = pathlib.Path(currentProjectPath)/"privateDB.db"
        connection = sqlite3.connect(privateDB)
        cursor = connection.cursor()
        cursor.execute("SELECT * FROM extracted_data")
        data = cursor.fetchall()
        connection.close()
        if data:
            return True
        else:
            return False

    def get_all_equipments(self, database):
        connection = sqlite3.connect(database)
        cursor = connection.cursor()
        cursor.execute("SELECT equipment_id, equipment_name FROM equipment")
        equipments = cursor.fetchall()
        connection.close()
        return equipments

    def format_data_for_yaml(self, zoneTextsLink):
        """Format data for YAML generation."""

        print(zoneTextsLink)
        
        # Dependencies
        links_map = getattr(self, "links_text_map", {})
        equipments = self.equipments
        
        if not zoneTextsLink:
            cprint("No zoneTextsLink provided, skipping YAML generation", "yellow")
            return
        
        all_group = {"nodes": {}}

        # Iterate zones -> build host_vars
        for zone_id, links_info in zoneTextsLink.items():
            # Determine hostname
            hostname_val = links_info.get('hostname')
            if not hostname_val:
                # Fallback if hostname is missing
                if not links_info.get('device'):
                    continue
                device_type = links_info.get('device') or equipments.get(zone_id, "device")
                hostname_val = f'{device_type}_{zone_id}'
            
            clean_hostname = hostname_val.strip()

            # Base host data with Ansible standard variables (snake_case)
            host_data = {
                "ansible_connection": "network_cli",
                "ansible_user": "<USERNAME>",
                "ansible_network_os": "ios",
                "hostname": clean_hostname,
                "device_type": links_info.get('device'),
                "interfaces": {}
            }

            device = links_info.get('device')
            
            # Helper to get protocol/vlan either from direct enriched dicts 
            # or falling back to self.links_text_map if available
            protocols_dict = links_info.get('protocols', {})
            vlans_dict = links_info.get('vlans', {})
            
            match device:
                case 'router':
                    # router interfaces
                    # links_info['interfaces'] is expected to be { link_id: interface_name }
                    for link_id, iface_name in links_info.get('interfaces', {}).items():
                        # Get mapped link info
                        link_meta = links_map.get(link_id, {})
                        
                        # Get IP
                        ip_addr = ""
                        if 'ip_addresses' in links_info and link_id in links_info['ip_addresses']:
                            ip_addr = links_info['ip_addresses'][link_id]
                        elif 'ip_add' in links_info and link_id in links_info['ip_add']:
                             ip_addr = links_info['ip_add'][link_id]

                        # Protocol/VLAN priorities:
                        # 1. Enriched 'protocols'/'vlans' dicts in links_info (user edits or pre-processing)
                        # 2. Lookup in self.links_text_map
                        
                        protocol_val = protocols_dict.get(link_id)
                        if protocol_val is None:
                             if link_meta.get('class') == 'protocol':
                                 protocol_val = link_meta.get('text')
                        
                        vlan_val = vlans_dict.get(link_id)
                        if vlan_val is None:
                             if link_meta.get('class') == 'vlan':
                                 vlan_val = link_meta.get('text')

                        host_data["interfaces"][iface_name] = {
                            "ip": ip_addr,
                            "protocol": protocol_val,
                            "vlan": vlan_val,
                            "status": 'up'
                        }

                case 'switch':
                    # switch interfaces
                    for link_id, iface_name in links_info.get('interfaces', {}).items():
                        
                        # Try to find VLAN info for this link
                        vlan_val = 'vlan1'
                        if link_id in vlans_dict:
                            vlan_val = vlans_dict[link_id]
                        elif 'vlan' in links_info and isinstance(links_info['vlan'], dict) and link_id in links_info['vlan']:
                             vlan_val = links_info['vlan'][link_id]
                        
                        host_data["interfaces"][iface_name] = {
                            'portMode': 'access',
                            'vlan': vlan_val
                        }

                case 'pc':
                    # PC often has just one IP
                    ip_val = links_info.get('ip_address')
                    iface_name = links_info.get('interface')
                    if not ip_val and 'ip_addresses' in links_info:
                         if links_info['ip_addresses']:
                             ip_val = list(links_info['ip_addresses'].values())[0]
                    
                    if ip_val:
                        if iface_name:
                            host_data['interfaces'][iface_name]['ip'] = ip_val
                        else:
                            host_data['interfaces']['eth0'] = {
                                'ip': ip_val
                            }
    
            all_group["nodes"][clean_hostname] = host_data
            data = all_group
        
        return data

    def save_data_to_yaml(self, data, currentProjectPath):
        """Save the formatted data into YAML files for Ansible.
        """

        project_path = pathlib.Path(currentProjectPath)
        print(project_path)
        group_vars_dir = project_path / "group_vars"
        host_vars_dir = project_path / "host_vars"
        group_vars_dir.mkdir(parents=True, exist_ok=True)
        host_vars_dir.mkdir(parents=True, exist_ok=True)

        data = data['nodes']

        for hostname, host_data in data.items():
            # Write individual host file
            host_vars_file = host_vars_dir / f"{hostname}.yml"
            with open(host_vars_file, "w", encoding="utf-8") as host_file:
                yaml.safe_dump(host_data, host_file, default_flow_style=False, sort_keys=False, allow_unicode=True)
                cprint(f"file {hostname}.yml successfully saved", 'green')

        # Write out group_vars/all.yml
        all_vars_path = project_path / "group_vars" / "all.yml"
        with open(all_vars_path, "w") as yaml_file:
            yaml.dump(data, yaml_file, default_flow_style=False)

        cprint("Data saved in the yaml files (host_vars + group_vars/all.yml)", 'green')

    def load_data_from_yaml(self, currentProjectPath):
        """Load data from group_vars/all.yml if it exists."""
        project_path = pathlib.Path(currentProjectPath)
        all_vars_path = project_path / "group_vars" / "all.yml"
        
        if not all_vars_path.exists():
            return None
            
        try:
            with open(all_vars_path, "r", encoding="utf-8") as f:
                nodes_data = yaml.safe_load(f)
            
            if nodes_data:
                return {"nodes": nodes_data}
            return None
        except Exception as e:
            cprint(f"Error loading YAML data: {e}", 'red')
            return None