import os
import sys
from PySide6.QtCore import QTime, QUrl, Qt, QThread, Signal, QObject
from PySide6.QtGui import QPixmap, QColor
from PySide6.QtWidgets import (QApplication, QMainWindow, QWidget, QDialog, QMessageBox, QStackedWidget,
    QGridLayout, QFileDialog, QVBoxLayout, QPushButton, QGroupBox, QLabel, QLineEdit, QFormLayout, QHBoxLayout, 
    QSizePolicy, QTableWidget, QTableWidgetItem, QHeaderView, QTextBrowser, QScrollArea)

from UI.main_window.main_window_ui import Ui_MainWindow
from UI.open_project.open_project_ui import Ui_OpenProject
from UI.image_import.image_import_ui import Ui_ImportImage
from UI.after_extraction.after_extraction_ui import Ui_AfterExtraction
from UI.modify_data.modify_ui import Ui_ModifyData
from UI.data_extracting.data_extracting_ui import Ui_DataExtraction
from UI.configs_content.configs_content_page import Ui_ConfigsContentPage

from logic.project import Project
from logic.topology_data import TopologyData
from logic.configurations import Configurations

from termcolor import cprint


class MainWindow(QMainWindow, Ui_MainWindow):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setupUi(self)
        self.appData = AppData()

        self.new_project_window = OpenProject(self)

        self.stacked = self.stackedWidget

        # Pages to be shown in the stacked widget
        self.after_extraction_page = AfterExtraction(self, appData=self.appData)
        self.project_open_page = OpenProject(self, appData=self.appData)

        # Ajout des pages au stacked widget
        self.stacked.addWidget(self.project_open_page)
        self.stacked.addWidget(self.after_extraction_page)

        # Affichage de la page de selection/creation d'un projet
        self.stacked.setCurrentWidget(self.project_open_page)
        # selectedPath, self.projectTitle = self.open_a_project()
        # self.currentProjectPath = Project().create_project(self.projectTitle, selectedPath)

        self.project_open_page.appInfosFilled.connect(self.on_project_created)

        # Page d'importation de l'image dans un projet
        # self.image_import_page_in_a_project = ImageImport(self, currentProjectPath=self.currentProjectPath, appData=self.appData)
        # self.stacked.addWidget(self.image_import_page_in_a_project)

        # self.stacked.setCurrentWidget(self.image_import_page_in_a_project)

        # Actions from the File menu
        self.actionNew.triggered.connect(self.trigger_new_project)
        self.actionOpen.triggered.connect(self.trigger_open_project)
        self.actionFermer.triggered.connect(self.close)
        self.actionSave.triggered.connect(self.save)
        self.actionSave_As.triggered.connect(self.save_as)

    def trigger_new_project(self):
        """Called when 'New' is clicked."""
        self.stacked.setCurrentWidget(self.project_open_page)

    def on_project_created(self):
        """Called when a new project is created via the form."""
        # Affichage de la page de selection/creation d'un projet
        self.currentProjectPath = str(self.appData.selectedDirectory)
        print(self.currentProjectPath)
        self.projectTitle = str(self.appData.projectTitle)
        print(self.projectTitle)
        self.appData.currentProjectPath = self.currentProjectPath
        
        # Reset data for new project
        self.appData.data = None
        self.appData.extracted = False

        self.import_image()

    
    
    def import_image(self):
        self.image_import_page_in_a_project = ImageImport(self, currentProjectPath=self.currentProjectPath, appData=self.appData)
        self.stacked.addWidget(self.image_import_page_in_a_project)
        self.stacked.setCurrentWidget(self.image_import_page_in_a_project)

        self.image_import_page_in_a_project.imageUploaded.connect(self.data_extraction)

    def trigger_open_project(self):
        """Called when 'Open' is clicked."""
        filePath, _ = QFileDialog.getOpenFileName(self, "Ouvrir un projet", "", "Projet Network (*.nmjnwa)")
        if not filePath:
            return
        self.load_project_from_file(filePath)

    def load_project_from_file(self, filePath):
        try:
            # Properly unpack 4 values from Project.load_project
            # Note: Project().load_project returns (projectFolder, imageFile, dbFile, configsPath)
            projectFolder, imageFile, dbFile, configsPath = Project().load_project(filePath)
            
            print(f"Loaded: {projectFolder}")

            self.currentProjectPath = projectFolder
            self.loadedProjectFile = filePath # Remember source zip

            # Update AppData
            self.appData.currentProjectPath = projectFolder
            self.appData.imagePath = imageFile 
            
            # Attempt to load data
            data = TopologyData().load_data_from_yaml(projectFolder)
            
            if data:
                self.appData.data = data
                self.appData.extracted = True
                print("Data loaded from YAML.")
                
                # Show AfterExtraction page
                self.stacked.setCurrentWidget(self.after_extraction_page)
                self.after_extraction_page.refresh_data()
            else:
                 print("No YAML data found. Showing extraction page (empty).")
                 self.appData.data = None 
                 self.appData.extracted = False
                 # If we have an image but no data, we are in state to run extraction.
                 # Go to AfterExtraction (it handles empty state) or potentially trigger extraction?
                 # Using AfterExtraction page is safer.
                 self.stacked.setCurrentWidget(self.after_extraction_page)
                 self.after_extraction_page.refresh_data()
        
        except Exception as e:
            import traceback
            traceback.print_exc()
            QMessageBox.critical(self, "Erreur", f"Impossible d'ouvrir le projet: {e}")

    def data_extraction(self):
        # The progress bar
        self.dataDialog = DataExtractAndConfigsGen(self)
        self.progressBar = self.dataDialog.progressBar
        self.progressBar.setMinimum(0)
        self.progressBar.setMaximum(0)
        self.dataDialog.show()

        self.worker = Worker(parent=None, appData=self.appData)
        self.thread = QThread()
        self.worker.moveToThread(self.thread)
        self.worker.finished.connect(self.thread.quit)  # Cleanly stop the thread when done
        self.worker.finished.connect(self.on_worker_finished)   # Call when finished
        self.thread.started.connect(self.worker.run)
        self.worker.message.connect(self.dataDialog.label.setText) # Connect status update
        self.thread.start()

        self.stacked.setCurrentWidget(self.after_extraction_page)
        
        # Connect the signal from AfterExtraction to MainWindow method
        self.after_extraction_page.configsGenerated.connect(self.show_generated_configs)

    def save(self):
        """
        Sauvegarde le projet en cours (Ecriture sur le disque).
        """
        if not self.appData.currentProjectPath:
            return

        print(f"Saving project at {self.appData.currentProjectPath}...")
        
        # Save YAML Data
        if self.appData.data:
            try:
                TopologyData().save_data_to_yaml(self.appData.data, self.appData.currentProjectPath)
                QMessageBox.information(self, "Succès", "Données sauvegardées sur le disque.")
            except Exception as e:
                print(f"Error saving YAML data: {e}")
                QMessageBox.warning(self, "Attention", f"Erreur lors de la sauvegarde des données: {e}")
        else:
             QMessageBox.information(self, "Info", "Aucune donnée topologique à sauvegarder.")

    def save_as(self):
        """Exporte le projet en cours vers un fichier .nmjnwa"""
        if not self.appData.currentProjectPath:
            QMessageBox.warning(self, "Attention", "Aucun projet n'est ouvert.")
            return

        filePath, _ = QFileDialog.getSaveFileName(self, "Enregistrer sous", "", "Projet Network (*.nmjnwa)")
        
        if not filePath:
            return

        try:
            Project().create_project_bundle(self.appData.currentProjectPath, filePath)
            QMessageBox.information(self, "Succès", f"Projet exporté vers {filePath}")
        except Exception as e:
            QMessageBox.critical(self, "Erreur", f"Echec de l'exportation: {e}")
    
    def on_worker_finished(self):
        """Called in the main thread when worker emits finished."""
        # close progress dialog if open
        if hasattr(self, "dataDialog") and self.dataDialog is not None:
            try:
                self.dataDialog.close()
            except Exception:
                pass
        # reset/hide progress bar
            self.progressBar.setMaximum(100)
            self.progressBar.setValue(100)

        # Refresh the AfterExtraction page now that data is ready
        if self.after_extraction_page:
            self.after_extraction_page.refresh_data()

    def after_extraction(self):
        self.stacked.setCurrentWidget(self.after_extraction_page)

    def show_generated_configs(self, projectPath):
        """Show the configurations page after generation."""
        # Instantiate the new ConfigContentPage which lists equipments
        self.configurations_page = ConfigsContentPage(self, projectFolder=projectPath)
        self.stacked.addWidget(self.configurations_page)
        self.stacked.setCurrentWidget(self.configurations_page)

    def closeEvent(self, event):
        # Demander de sauvegarder avant de fermer
        confirmation = QMessageBox.question(self, "Confirmation", "Voulez-vous Sauvegarder avant de Fermer l'Application ?", QMessageBox.Yes | QMessageBox.No)

        if confirmation == QMessageBox.Yes:
            self.save()
            event.accept()
        else:
            event.accept()


class OpenProject(QDialog, Ui_OpenProject):
    appInfosFilled = Signal()
    def __init__(self, parent=None, projectPath=None, appData=None):
        super().__init__(parent)
        self.setupUi(self)
        self.appData = appData
        self.projectPath = projectPath

        self.destination_paths_combo()

        self.createProject = self.createProjectButton
        self.createProject.pressed.connect(self.create_new_project)
        self.changeFileDestinationButton.pressed.connect(self.select_project_destination)
        self.closeProjectFormButton.pressed.connect(self.close)
        self.openAProjectButton.pressed.connect(self.select_project)

    def destination_paths_combo(self):
        project = Project()
        destinationPaths = project.get_all_destination_paths()
        if (destinationPaths):
            self.projectDestinationCombo.addItems([path[1] for path in destinationPaths])
            self.projectDestinationCombo.setCurrentText(destinationPaths[-1][1])

    def create_new_project(self):
        projectTitle = self.projectTitleField.text()
        projectDestination = self.projectDestinationCombo.currentText()

        if projectTitle and projectDestination:
            projectDirectory = Project().create_project(projectTitle, projectDestination)
            self.close()
            self.appData.projectTitle = projectTitle
            self.appData.selectedDirectory = projectDirectory
            self.appInfosFilled.emit()

    def select_project_destination(self):
        directory = QFileDialog.getExistingDirectory(self, "Select Project Destination")
        if directory:
            Project().save_destination_path_to_db(directory)
            self.projectDestinationCombo.clear()
            self.destination_paths_combo()
            self.projectDestinationCombo.setCurrentText(directory)

    def select_project(self):
        # Trigger the main window's open logic
        if self.parent():
            self.parent().trigger_open_project()


class ImageImport(QWidget, Ui_ImportImage):
    imageUploaded = Signal()
    def __init__(self, parent=None, currentProjectPath=None, appData=None):
        super().__init__(parent)
        self.setupUi(self)
        self.appData = appData
        self.currentProjectPath = currentProjectPath

        self.importImageButton.pressed.connect(self.import_image)


    def import_image(self):
        filePath, _ = QFileDialog.getOpenFileName(self, "Select an image", "", "Images (*.png *.jpg *.jpeg *.bmp *.gif)")
        if filePath:
            self.appData.imagePath = filePath
            self.imageUploaded.emit()
         

class AfterExtraction(QWidget, Ui_AfterExtraction):
    configsGenerated = Signal(str) # Signal to notify that configs are ready, passing project path
    def __init__(self, parent=None, appData=None):
        super().__init__(parent)
        self.setupUi(self)
        self.appData = appData
        self.tables = {}

        # Connect buttons
        self.pushButton.hide()
        self.pushButton_2.setText("Appliquer les modifications")
        self.pushButton_2.clicked.connect(self.apply_modifications)
        self.pushButton_3.clicked.connect(self.generate_and_continue)
        
        # Initial show
        self.show_extracted_data()

    def showEvent(self, event):
        """Called when widget is shown. Refresh data."""
        self.refresh_data()
        super().showEvent(event)

    def resizeEvent(self, event):
        """Handle window resize to show/hide image."""
        if self.width() > 1200:
            if hasattr(self, 'image_scroll_area'):
                self.image_scroll_area.show()
        else:
            if hasattr(self, 'image_scroll_area'):
                self.image_scroll_area.hide()
        super().resizeEvent(event)

    def refresh_data(self):
        self.show_extracted_data()
        self.show_image()

    def show_image(self):
        """Display the source image if available."""
        if self.appData.imagePath and os.path.exists(self.appData.imagePath):
            pixmap = QPixmap(self.appData.imagePath)
            if not pixmap.isNull():
                if not hasattr(self, 'image_label'):
                     # Create image viewer if not exists
                    self.image_label = QLabel()
                    self.image_label.setAlignment(Qt.AlignCenter)
                    self.image_scroll_area = QScrollArea()
                    self.image_scroll_area.setWidget(self.image_label)
                    self.image_scroll_area.setWidgetResizable(True)
                    # Add to layout to the right of the existing groupbox
                    # gridLayout_3 contains the main layout of AfterExtraction
                    # internal structure: widget(0,0->Title), groupBox(1,0->Data)
                    self.gridLayout_3.addWidget(self.image_scroll_area, 1, 1)
                
                self.image_label.setPixmap(pixmap)
                
                # Check visibility based on current size
                if self.width() > 1200:
                    self.image_scroll_area.show()
                else:
                    self.image_scroll_area.hide()

    def _clear_layout(self, layout):
        while layout.count():
            item = layout.takeAt(0)
            widget = item.widget()
            if widget:
                widget.deleteLater()
            else:
                child_layout = item.layout()
                if child_layout:
                    self._clear_layout(child_layout)

    def show_extracted_data(self):
        """Loop through self.appData.data and show it in tables."""
        container_layout = self.verticalLayout_2
        self._clear_layout(container_layout)
        self.tables = {}

        self.data = self.appData.data
            
        # If extraction hasn't happened or finished yet, just return
        if not self.appData.extracted or not self.data or 'nodes' not in self.data:
            container_layout.addWidget(QLabel("Data not yet extracted. Please open a project and run extraction."))
            container_layout.addStretch()
            return

        # Iterate over nodes in data
        # Structure is data['nodes'][hostname] = { ... }
        cprint("Data to show", 'blue')
        print(self.data)
        cprint("-------------------------\n", 'blue')
        for hostname, node_infos in self.data['nodes'].items():
            # Header Title = Hostname
            title_label = QLabel(hostname)
            title_label.setStyleSheet("font-weight: bold; font-size: 14px; margin-top: 10px;")
            container_layout.addWidget(title_label)

            column_created = False
            row_data = []
            link_mapping = [] # List to store raw interface key for identification

            device_type = node_infos.get("device", "")

            # routers / switches
            interfaces = node_infos.get("interfaces", {})

            # Initialize columns default
            columns = [] 
                
            for if_name, if_data in interfaces.items():
                if not column_created:
                    columns = [key for key in if_data.keys()] # Store the keys in from the interface dictionnaty as the columns names
                    columns.insert(0, 'interfaces')
                    column_created = True

                i_data = [value for value in if_data.values()]
                # print(i_data)
                i_data.insert(0, if_name)
                row_data.append(i_data) # Store every interface with the data to be printed for it
                link_mapping.append(if_name) # Store the key used in dict

            # print(row_data)

            print(columns)
            
            # Create Table
            table = QTableWidget()
            table.setColumnCount(len(columns))
            table.setHorizontalHeaderLabels(columns)
            table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
            
            # Style header
            header = table.horizontalHeader()
            header.setStyleSheet("QHeaderView::section { background-color: #C39BD3; color: black; font-weight: bold; }")

            table.setRowCount(len(row_data))
            
            for row_idx, row_text in enumerate(row_data):
                # print(list(row_text))
                # Interface
                # item_if = QTableWidgetItem(str(if_txt))
                # # Store original interface key to identify it during save
                # item_if.setData(Qt.UserRole, link_mapping[row_idx]) 
                # table.setItem(row_idx, 0, item_if)
                
                # # IP
                # item_ip = QTableWidgetItem(str(ip_txt))
                # table.setItem(row_idx, 1, item_ip)
                
                # # Protocol
                # item_proto = QTableWidgetItem(str(proto_txt))
                # table.setItem(row_idx, 2, item_proto)

                # # Device Type
                # item_device = QTableWidgetItem(str(device_txt))
                # # item_device.setFlags(item_device.flags() ^ Qt.ItemIsEditable) # Optional: make read-only if desired
                # table.setItem(row_idx, 3, item_device)

                col = 0
                for text in row_text:
                    item = QTableWidgetItem(str(text))
                    item.setData(Qt.UserRole, link_mapping[row_idx])
                    table.setItem(row_idx, col, item)
                    col += 1

            # Adjust height: header height + row height * rows + padding
            row_h = table.verticalHeader().defaultSectionSize()
            hdr_h = table.horizontalHeader().height()
            total_h = hdr_h + (row_h * len(row_data)) + 50
            table.setMinimumHeight(min(300, total_h)) 
            
            container_layout.addWidget(table)
            
            # Store table with hostname for saving
            self.tables[hostname] = table

        container_layout.addStretch()

    def apply_modifications(self):
        """Read data from tables, update appData.data, and save."""
        data = self.data
        if not data or 'nodes' not in data:
            return

        for hostname, table in self.tables.items():
            if hostname not in data['nodes']: continue
            
            node_infos = data['nodes'][hostname]
            device_type = node_infos.get("device")
            
            rows = table.rowCount()
            for r in range(rows):
                item_if = table.item(r, 0)
                item_ip = table.item(r, 1)
                item_proto = table.item(r, 2)
                
                original_if_key = item_if.data(Qt.UserRole)
                
                new_if_name = item_if.text().strip()
                new_ip = item_ip.text().strip()
                new_proto_str = item_proto.text().strip()
                
                # Parse Protocol/VLAN from string (simple parsing)
                # Format: "proto_val | VLAN: vlan_val" or just "proto_val" or "VLAN: vlan_val"
                new_protocol = None
                new_vlan = None
                
                parts = new_proto_str.split('|')
                for part in parts:
                    part = part.strip()
                    if part.lower().startswith("vlan:"):
                        new_vlan = part.split(":", 1)[1].strip()
                    elif part:
                        new_protocol = part
                
                if device_type == 'pc':
                    # Single IP update usually
                    if new_ip:
                        node_infos['ip'] = new_ip
                else:
                    # Update interfaces
                    interfaces = node_infos.get('interfaces', {})
                    
                    # If interface name changed, we handle key rename
                    target_key = original_if_key
                    if new_if_name != original_if_key:
                        # User renamed interface
                        if original_if_key in interfaces:
                            # Pop old, create new
                            if_data = interfaces.pop(original_if_key)
                            interfaces[new_if_name] = if_data
                            target_key = new_if_name
                    
                    # Ensure target exists (if it was new? we don't support Adding rows yet easily, but for renaming)
                    if target_key not in interfaces:
                        interfaces[target_key] = {}
                    
                    # Update Content
                    interfaces[target_key]['ip'] = new_ip
                    if new_protocol:
                        interfaces[target_key]['protocol'] = new_protocol
                    if new_vlan:
                        interfaces[target_key]['vlan'] = new_vlan

        self.data.data = data

    def generate_and_continue(self):
        """Generate configurations and move to the next page."""
        # Call save function
        try:
            TopologyData().save_data_to_yaml(self.data, self.appData.currentProjectPath)
            # QMessageBox.information(self, "Succès", "Variables sauvegardees avec succes")
        except Exception as e:
            QMessageBox.critical(self, "Erreur", f"Erreur lors de la sauvegarde: {str(e)}")
            return

        # Show progress dialog
        self.configsGen = DataExtractAndConfigsGen(self)
        self.configsGen.label.setText("Starting configuration generation...")
        self.progressBar = self.configsGen.progressBar
        self.progressBar.setMinimum(0)
        self.progressBar.setMaximum(100) # Percentage based
        self.progressBar.setValue(0)
        self.configsGen.show()

        # Start Worker
        self.worker = ConfigsGenWorker(parent=None, appData=self.appData)
        self.thread = QThread()
        self.worker.moveToThread(self.thread)
        self.worker.finished.connect(self.thread.quit)
        self.worker.finished.connect(self.on_config_worker_finished)
        self.thread.started.connect(self.worker.run)
        self.worker.message.connect(self.configsGen.label.setText)
        self.worker.progress.connect(self.progressBar.setValue)
        self.thread.start()

    def on_config_worker_finished(self):
        """Called when config worker finishes."""
        if hasattr(self, "configsGen") and self.configsGen is not None:
            self.configsGen.close()
        
        QMessageBox.information(self, "Succès", "Configurations générées avec succès.")
        self.configsGenerated.emit(self.appData.currentProjectPath)
        

class ConfigsContentPage(QWidget, Ui_ConfigsContentPage):
    """
    Page to list all equipments and allow viewing their configurations.
    Uses a split view: List of Equipments (Left) and Configuration Content (Right).
    """
    def __init__(self, parent=None, projectFolder=None):
        super().__init__(parent)
        self.projectFolder = projectFolder
        self.setupUi(self)
        
        # Connect signals
        self.equipmentList.currentItemChanged.connect(self.display_configuration)
        
        self.show_equipments()
        self.display_placeholder()

    def show_equipments(self):
        """Populate the left pane with equipment names."""
        self.equipmentList.clear()
        equipments = []
        
        if self.projectFolder:
            # Check for generated configurations
            config_dir = os.path.join(self.projectFolder, "configurations")
            if os.path.exists(config_dir):
                files = [f for f in os.listdir(config_dir) if f.endswith(".cfg")]
                equipments = [os.path.splitext(f)[0] for f in files]
        
        # Sort for better UX
        equipments.sort()

        if equipments:
            self.equipmentList.addItems(equipments)
        else:
            self.equipmentList.addItem("No configurations found.")
            self.equipmentList.setEnabled(False)

    def display_configuration(self, current, previous):
        """Show configuration content in the right pane."""
        if not current:
            return
            
        equipment_name = current.text()
        
        if not self.projectFolder:
            self.configTextEdit.setText("Project folder not set.")
            return

        config_path = os.path.join(self.projectFolder, "configurations", f"{equipment_name}.cfg")
        
        if os.path.exists(config_path):
            with open(config_path, "r", encoding="utf-8") as f:
                content = f.read()
            self.configTextEdit.setText(content)
        else:
            self.configTextEdit.setText(f"Error: Configuration file for {equipment_name} not found.")

    def display_placeholder(self):
         self.configTextEdit.setText("Select an equipment to view its configuration.")


class DataExtractAndConfigsGen(QDialog, Ui_DataExtraction):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setupUi(self)


class Worker(QObject):
    def __init__(self, parent=None, appData=None):
        super().__init__(parent)
        self.appData = appData
        self.imagePath = self.appData.imagePath
        self.currentProjectPath = self.appData.currentProjectPath

    progress = Signal(int)
    finished = Signal()
    message = Signal(str)

    def run(self):
        def callback(msg):
             self.message.emit(msg)
        
        self.appData.data = TopologyData().process(self.imagePath, self.currentProjectPath, status_callback=callback)
        self.appData.extracted = True
        self.finished.emit()


class ConfigsGenWorker(QObject):
    def __init__(self, parent=None, appData=None):
        super().__init__(parent)
        self.data = appData.data
        self.currentProjectPath = appData.currentProjectPath

    finished = Signal()
    message = Signal(str)
    progress = Signal(int)

    def run(self):
        def callback(msg):
             self.message.emit(msg)
        
        def progress_callback(percent):
             self.progress.emit(percent)

        try:
            config_gen = Configurations(self.currentProjectPath, self.data)
            config_gen.generate_configurations(data=self.data, status_callback=callback, progress_callback=progress_callback)
            callback("Configurations generated.")
        except Exception as e:
            callback(f"Error: {e}")
            print(f"Error generating configs: {e}")
        
        self.finished.emit()


class AppData:
    def __init__(self):
        self.imagePath = None
        self.selectedDirectory = None
        self.projectTitle = None
        self.currentProjectPath = None
        self.extracted = False
        self.data = None


app = QApplication(sys.argv)
main_window = MainWindow()
main_window.show()
sys.exit(app.exec())