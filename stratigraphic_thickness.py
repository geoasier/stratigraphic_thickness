# Importaciones necesarias
from qgis.PyQt.QtWidgets import (QDialog, QVBoxLayout, QLabel, QLineEdit, QPushButton, QMessageBox, QAction, QComboBox)
from qgis.PyQt.QtCore import Qt, QSettings
from qgis.PyQt.QtGui import QIcon  # Importar QIcon para el logo
from qgis.core import QgsRasterLayer, QgsProject, QgsPointXY, QgsRaster, QgsGeometry, QgsWkbTypes
from qgis.gui import QgsMapToolEmitPoint, QgsRubberBand
import math

# Importar recursos (asegúrate de que resources.py esté en la misma carpeta)
from . import resources


# Clase del diálogo
class StratigraphicThicknessDialog(QDialog):
    def __init__(self, iface):
        super().__init__()
        self.iface = iface
        self.setWindowTitle("Stratigraphic Thickness Calculator")
        self.setLayout(QVBoxLayout())

        # Hacer que la ventana permanezca en primer plano
        self.setWindowFlags(self.windowFlags() | Qt.WindowStaysOnTopHint)

        # Configuración para guardar y cargar preferencias
        self.settings = QSettings("YourOrganization", "StratigraphicThicknessCalculator")

        # Menú desplegable para seleccionar el MDT
        self.label_dem = QLabel("Select DEM:")
        self.combo_dem = QComboBox()
        self.layout().addWidget(self.label_dem)
        self.layout().addWidget(self.combo_dem)

        # Campo para introducir el buzamiento
        self.label_alpha = QLabel("Dip angle (degrees, 0 < α < 90):")
        self.input_alpha = QLineEdit()
        self.input_alpha.setToolTip("Enter the dip angle in degrees (must be between 0 and 90).")
        self.layout().addWidget(self.label_alpha)
        self.layout().addWidget(self.input_alpha)

        # Etiquetas para mostrar los resultados
        self.label_top_height = QLabel("Top Height: -")
        self.label_base_height = QLabel("Base Height: -")
        self.label_horizontal_distance = QLabel("Horizontal Distance: -")
        self.label_stratigraphic_thickness = QLabel("Stratigraphic Thickness: -")
        self.label_stratigraphic_thickness.setStyleSheet("font-weight: bold;")  # Texto en negrita

        # Añadir las etiquetas al layout
        self.layout().addWidget(self.label_top_height)
        self.layout().addWidget(self.label_base_height)
        self.layout().addWidget(self.label_horizontal_distance)
        self.layout().addWidget(self.label_stratigraphic_thickness)

        # Cargar los rasters disponibles en el menú desplegable
        self.load_rasters()

        # Cargar preferencias guardadas
        self.load_preferences()

        # Herramienta para hacer clic en el mapa
        self.map_tool = QgsMapToolEmitPoint(self.iface.mapCanvas())
        self.map_tool.canvasClicked.connect(self.get_point)
        self.map_tool.setCursor(Qt.CrossCursor)
        self.line_points = []  # Lista de puntos de la línea

        # RubberBands para dibujar puntos y líneas
        self.rubber_band_line = QgsRubberBand(self.iface.mapCanvas(), QgsWkbTypes.LineGeometry)
        self.rubber_band_line.setColor(Qt.red)  # Color rojo intenso
        self.rubber_band_line.setWidth(2)  # Línea más gruesa

        self.rubber_band_points = QgsRubberBand(self.iface.mapCanvas(), QgsWkbTypes.PointGeometry)
        self.rubber_band_points.setColor(Qt.red)  # Color rojo intenso
        self.rubber_band_points.setIconSize(10)  # Puntos más grandes

        # RubberBand temporal para la línea que sigue el cursor
        self.rubber_band_temp = QgsRubberBand(self.iface.mapCanvas(), QgsWkbTypes.LineGeometry)
        self.rubber_band_temp.setColor(Qt.red)  # Color rojo intenso
        self.rubber_band_temp.setWidth(2)  # Línea más gruesa
        self.rubber_band_temp.setLineStyle(Qt.DashLine)  # Línea discontinua

        # Conectar el movimiento del cursor para dibujar la línea temporal
        self.iface.mapCanvas().xyCoordinates.connect(self.update_temp_line)

    def load_rasters(self):
        """Carga los rasters disponibles en el menú desplegable."""
        self.combo_dem.clear()
        for layer in QgsProject.instance().mapLayers().values():
            if isinstance(layer, QgsRasterLayer):
                self.combo_dem.addItem(layer.name(), layer)

    def load_preferences(self):
        """Carga las preferencias guardadas (DEM seleccionado y ángulo de buzamiento)."""
        last_dem = self.settings.value("last_dem", "")
        last_alpha = self.settings.value("last_alpha", "")

        # Seleccionar el DEM guardado
        if last_dem:
            index = self.combo_dem.findText(last_dem)
            if index >= 0:
                self.combo_dem.setCurrentIndex(index)

        # Cargar el ángulo de buzamiento guardado
        if last_alpha:
            self.input_alpha.setText(last_alpha)

    def save_preferences(self):
        """Guarda las preferencias (DEM seleccionado y ángulo de buzamiento)."""
        self.settings.setValue("last_dem", self.combo_dem.currentText())
        self.settings.setValue("last_alpha", self.input_alpha.text())

    def get_point(self, point, button):
        """Obtiene las coordenadas y alturas al hacer clic en el mapa."""
        if button == Qt.LeftButton:
            # Añadir punto a la línea
            self.line_points.append(point)
            self.rubber_band_points.addPoint(point)  # Dibujar punto

            # Si hay dos puntos, dibujar la línea y calcular el espesor
            if len(self.line_points) == 2:
                self.draw_line()
                self.calculate_thickness()
            # Si hay un tercer clic, reiniciar la medición
            elif len(self.line_points) == 3:
                self.reset_measurement()
        elif button == Qt.RightButton:
            # Reiniciar la medición
            self.reset_measurement()

    def draw_line(self):
        """Dibuja la línea entre los dos puntos."""
        if len(self.line_points) == 2:
            self.rubber_band_line.reset()
            line = QgsGeometry.fromPolylineXY(self.line_points)
            self.rubber_band_line.setToGeometry(line, None)

    def update_temp_line(self, point):
        """Actualiza la línea temporal mientras se mueve el cursor."""
        if len(self.line_points) == 1:
            self.rubber_band_temp.reset()
            temp_line = QgsGeometry.fromPolylineXY([self.line_points[0], point])
            self.rubber_band_temp.setToGeometry(temp_line, None)

    def calculate_thickness(self):
        """Calcula el espesor estratigráfico."""
        try:
            # Validar que se haya seleccionado un DEM
            dem_layer = self.combo_dem.currentData()
            if dem_layer is None:
                QMessageBox.warning(self, "Error", "Please select a DEM layer.")
                return

            # Validar que el ángulo de buzamiento sea un número válido
            alpha = float(self.input_alpha.text())
            if alpha <= 0 or alpha >= 90:
                QMessageBox.warning(self, "Error", "Dip angle must be between 0 and 90 degrees.")
                return

            # Obtener los dos puntos de la línea
            start_point = self.line_points[0]
            end_point = self.line_points[1]

            # Validar que los puntos sean diferentes
            if start_point == end_point:
                QMessageBox.warning(self, "Error", "Start and end points must be different.")
                return

            # Obtener alturas
            ai, af = self.get_heights(start_point, end_point)
            if ai is None or af is None:
                return

            # Obtener distancia horizontal
            d = start_point.distance(end_point)

            # Convertir buzamiento a radianes
            alpha_rad = math.radians(alpha)

            # Calcular diferencia de alturas
            delta_h = ai - af

            # Calcular espesor estratigráfico
            E = d * math.sin(alpha_rad) - delta_h * math.cos(alpha_rad)

            # Mostrar los resultados en varias líneas
            self.label_top_height.setText(f"Top Height: {af:.2f} m")
            self.label_base_height.setText(f"Base Height: {ai:.2f} m")
            self.label_horizontal_distance.setText(f"Horizontal Distance: {d:.2f} m")
            self.label_stratigraphic_thickness.setText(f"Stratigraphic Thickness: {E:.2f} m")
        except ValueError:
            QMessageBox.warning(self, "Error", "Please enter valid numeric values.")

    def get_heights(self, start_point, end_point):
        """Obtiene las alturas inicial y final del MDT seleccionado."""
        dem_layer = self.combo_dem.currentData()
        if dem_layer is None:
            QMessageBox.warning(self, "Error", "Please select a DEM layer.")
            return None, None

        # Obtener alturas
        ai = self.get_height_from_dem(dem_layer, start_point)
        af = self.get_height_from_dem(dem_layer, end_point)

        if ai is not None and af is not None:
            return ai, af
        else:
            QMessageBox.warning(self, "Error", "Could not retrieve heights from the DEM.")
            return None, None

    def get_height_from_dem(self, dem_layer, point):
        """Obtiene la altura de un punto en el MDT."""
        ident = dem_layer.dataProvider().identify(point, QgsRaster.IdentifyFormatValue)
        if ident.isValid():
            height = ident.results().get(1)  # Suponiendo que la altura está en la banda 1
            if height is not None:
                return float(height)
        QMessageBox.warning(self, "Error", f"Could not retrieve height for point {point}.")
        return None

    def reset_measurement(self):
        """Reinicia la medición para permitir una nueva."""
        self.line_points = []
        self.label_top_height.setText("Top Height: -")
        self.label_base_height.setText("Base Height: -")
        self.label_horizontal_distance.setText("Horizontal Distance: -")
        self.label_stratigraphic_thickness.setText("Stratigraphic Thickness: -")
        self.rubber_band_line.reset(QgsWkbTypes.LineGeometry)  # Limpiar RubberBand de línea
        self.rubber_band_points.reset(QgsWkbTypes.PointGeometry)  # Limpiar RubberBand de puntos
        self.rubber_band_temp.reset(QgsWkbTypes.LineGeometry)  # Limpiar RubberBand temporal

    def closeEvent(self, event):
        """Guarda las preferencias, limpia las RubberBands y desactiva la herramienta al cerrar el diálogo."""
        self.save_preferences()
        self.rubber_band_line.reset()
        self.rubber_band_points.reset()
        self.rubber_band_temp.reset()
        self.iface.mapCanvas().xyCoordinates.disconnect(self.update_temp_line)  # Desconectar el movimiento del cursor
        self.iface.mapCanvas().unsetMapTool(self.map_tool)  # Desactivar la herramienta
        super().closeEvent(event)


# Clase principal del plugin
class StratigraphicThickness:
    def __init__(self, iface):
        self.iface = iface

    def initGui(self):
        # Crear acción para el plugin
        self.action = QAction(
            QIcon(":/plugins/stratigraphic_thickness/icon.png"),  # Ruta del logo
            "Stratigraphic Thickness",  # Nombre del plugin
            self.iface.mainWindow()
        )
        self.action.triggered.connect(self.run)
        
        # Agregar botón a la barra de herramientas y menú
        self.iface.addToolBarIcon(self.action)
        self.iface.addPluginToMenu("&Stratigraphic Thickness", self.action)

    def unload(self):
        # Eliminar botón de la barra de herramientas y menú
        self.iface.removeToolBarIcon(self.action)
        self.iface.removePluginMenu("&Stratigraphic Thickness", self.action)

    def run(self):
        # Crear y mostrar el diálogo
        self.dialog = StratigraphicThicknessDialog(self.iface)
        self.dialog.show()
        # Activar la herramienta de clic en el mapa
        self.iface.mapCanvas().setMapTool(self.dialog.map_tool)