# -*- coding: utf-8 -*-
"""
Stratigraphic Thickness Calculator
-----------------------------------
Estima el espesor estratigrafico de uno o varios tramos seleccionados sobre
el mapa, mediante el clasico calculo trigonometrico usado en campo
(distancia horizontal + buzamiento, corregido con el desnivel del DEM).

Metodo valido para series continuas sin deformacion tectonica: se empieza
por la base de la serie y cada clic nuevo cierra un segmento y abre el
siguiente, permitiendo encadenar varios tramos (por ejemplo, separados por
zonas cubiertas), cada uno con su propio buzamiento.
"""

from qgis.PyQt.QtWidgets import (
    QDialog, QVBoxLayout, QLabel,
    QMessageBox, QAction, QComboBox, QListWidget, QListWidgetItem,
    QShortcut, QInputDialog, QCheckBox
)
from qgis.PyQt.QtCore import Qt, QSettings
from qgis.PyQt.QtGui import QIcon, QColor, QKeySequence
from qgis.core import (
    QgsRasterLayer, QgsProject, QgsGeometry, QgsWkbTypes,
    QgsRaster, QgsCoordinateTransform, QgsDistanceArea
)
from qgis.gui import QgsMapToolEmitPoint, QgsRubberBand
import math

# Importar recursos (asegurate de que resources.py este en la misma carpeta)
from . import resources

DEFAULT_DIP_ANGLE = 45.0


class StratigraphicThicknessDialog(QDialog):
    """Ventana principal del plugin."""

    def __init__(self, iface):
        super().__init__()
        self.iface = iface
        self.setWindowTitle("Stratigraphic Thickness Calculator")
        self.setLayout(QVBoxLayout())
        self.setWindowFlags(self.windowFlags() | Qt.WindowType.WindowStaysOnTopHint)

        self.settings = QSettings("EHU", "StratigraphicThicknessCalculator")

        # Estado de la medicion en curso
        self.line_points = []       # todos los puntos clicados de la traza
        self.point_elevations = []  # elevacion de cada punto (dem/manual/none), en paralelo a line_points
        self.segments = []          # un dict por cada segmento ya cerrado
        self.last_alpha_used = DEFAULT_DIP_ANGLE
        self.last_manual_elevation = 0.0
        self.is_geographic_crs = False
        self.last_thickness_negative = False

        self._build_ui()
        self._build_map_tools()
        self.setMinimumWidth(300)
        self.setMaximumWidth(340)

        self.load_rasters()
        self.load_preferences()
        self.check_crs_warning()
        self.on_elevation_source_changed()

        # Mantener el combo de DEM al dia si se anaden/quitan capas
        QgsProject.instance().layersAdded.connect(self.on_layers_changed)
        QgsProject.instance().layersRemoved.connect(self.on_layers_changed)
        self.iface.mapCanvas().destinationCrsChanged.connect(self.check_crs_warning)
        self.iface.mapCanvas().xyCoordinates.connect(self.update_temp_line)

    # ------------------------------------------------------------------
    # Construccion de la interfaz
    # ------------------------------------------------------------------

    # Estilos reutilizables para la píldora de aviso (solo una a la vez)
    PILL_AMBER = ("background-color:#fdf2e3; border:1px solid #b9770e; border-radius:6px; "
                  "padding:4px 8px; color:#b9770e; font-weight:bold; font-size:11px;")
    PILL_BLUE = ("background-color:#eaf2f8; border:1px solid #2980b9; border-radius:6px; "
                 "padding:4px 8px; color:#2980b9; font-weight:bold; font-size:11px;")
    PILL_RED = ("background-color:#fdecea; border:1px solid #c0392b; border-radius:6px; "
                "padding:4px 8px; color:#c0392b; font-weight:bold; font-size:11px;")

    def _build_ui(self):
        layout = self.layout()
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(6)

        # Instrucciones fijas de uso, breves y directas.
        instructions = QLabel(
            "Start at the base of the succession \u2014 each click closes a "
            "segment and opens the next. Double-click a segment in the list "
            "below to edit its dip without redrawing it."
        )
        instructions.setStyleSheet("font-style: italic; color: #555; font-size: 11px;")
        instructions.setWordWrap(True)
        layout.addWidget(instructions)

        layout.addWidget(QLabel("Elevation source:"))
        self.combo_elevation_source = QComboBox()
        self.combo_elevation_source.addItem("DEM layer", "dem")
        self.combo_elevation_source.addItem("Manual elevation", "manual")
        self.combo_elevation_source.addItem("No elevation", "none")
        self.combo_elevation_source.currentIndexChanged.connect(self.on_elevation_source_changed)
        layout.addWidget(self.combo_elevation_source)

        self.label_dem = QLabel("Select DEM:")
        layout.addWidget(self.label_dem)
        self.combo_dem = QComboBox()
        layout.addWidget(self.combo_dem)

        # Modo subhorizontal: para capas con buzamiento muy bajo, en vez de
        # trazar una linea con angulo, se toma directamente la diferencia de
        # cota entre el punto de base y el de techo (mismo resultado que da
        # la formula general cuando el buzamiento tiende a 0).
        self.checkbox_subhorizontal = QCheckBox("(Sub)horizontal mode (dip \u22645\u00ba)")
        self.checkbox_subhorizontal.setToolTip(
            "For beds with dip equal to or lower than 5\u00b0. Skips the dip "
            "question: click the base point and the top point, and the "
            "thickness is taken directly as their elevation difference."
        )
        self.checkbox_subhorizontal.stateChanged.connect(self.update_status_pill)
        layout.addWidget(self.checkbox_subhorizontal)

        # Una unica pildora de aviso: nunca se muestra mas de un mensaje a
        # la vez (prioridad: espesor negativo > sin alturas > subhorizontal
        # > CRS geografico), asi se evita el solape de varios avisos.
        self.label_status_pill = QLabel("")
        self.label_status_pill.setWordWrap(True)
        self.label_status_pill.setVisible(False)
        layout.addWidget(self.label_status_pill)

        # Base / techo / distancia del ultimo segmento, en una sola linea
        self.label_segment_info = QLabel("Base: -   Top: -   Distance: -")
        self.label_segment_info.setStyleSheet("font-size: 11px; color: #333;")
        layout.addWidget(self.label_segment_info)

        layout.addWidget(QLabel("Segments:"))
        self.list_segments = QListWidget()
        self.list_segments.setMaximumHeight(90)
        self.list_segments.setToolTip("Double-click a segment to edit its dip angle.")
        self.list_segments.itemDoubleClicked.connect(self.edit_segment_dip)
        layout.addWidget(self.list_segments)

        self.label_total = QLabel("TOTAL THICKNESS: -")
        self.label_total.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.label_total.setStyleSheet(
            "background-color:#eaf2f8; border:1px solid #2980b9; border-radius:6px; "
            "padding:5px; font-weight:bold; font-size:14px; color:#1b4f72;"
        )
        layout.addWidget(self.label_total)

        hint = QLabel("\U0001F5B1\ufe0f Right-click the map to reset  \u00b7  \u232b Backspace to undo last point")
        hint.setStyleSheet("color: #888; font-size: 10px;")
        hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(hint)

    def _build_map_tools(self):
        self.map_tool = QgsMapToolEmitPoint(self.iface.mapCanvas())
        self.map_tool.canvasClicked.connect(self.get_point)
        self.map_tool.setCursor(Qt.CursorShape.CrossCursor)

        # Colores: azul para la traza, naranja para los puntos (el rojo se
        # deja libre para los avisos, así no se confunden con "algo va mal").
        line_color = QColor("#2980b9")
        point_color = QColor("#e67e22")
        halo_color = QColor(255, 255, 255, 235)

        # Halo blanco debajo de la línea, para que se lea sobre cualquier capa
        self.rubber_band_line_halo = QgsRubberBand(self.iface.mapCanvas(), QgsWkbTypes.GeometryType.LineGeometry)
        self.rubber_band_line_halo.setColor(halo_color)
        self.rubber_band_line_halo.setWidth(5)

        self.rubber_band_line = QgsRubberBand(self.iface.mapCanvas(), QgsWkbTypes.GeometryType.LineGeometry)
        self.rubber_band_line.setColor(line_color)
        self.rubber_band_line.setWidth(2)

        # Halo blanco debajo de cada punto, mismo motivo
        self.rubber_band_points_halo = QgsRubberBand(self.iface.mapCanvas(), QgsWkbTypes.GeometryType.PointGeometry)
        self.rubber_band_points_halo.setColor(halo_color)
        self.rubber_band_points_halo.setIcon(QgsRubberBand.IconType.ICON_CIRCLE)
        self.rubber_band_points_halo.setIconSize(14)

        self.rubber_band_points = QgsRubberBand(self.iface.mapCanvas(), QgsWkbTypes.GeometryType.PointGeometry)
        self.rubber_band_points.setColor(point_color)
        self.rubber_band_points.setIcon(QgsRubberBand.IconType.ICON_CIRCLE)
        self.rubber_band_points.setIconSize(8)

        # Línea de vista previa: punteada ("suspensiva") pero con contraste
        # suficiente para verse bien sobre cualquier capa base.
        self.rubber_band_temp = QgsRubberBand(self.iface.mapCanvas(), QgsWkbTypes.GeometryType.LineGeometry)
        self.rubber_band_temp.setColor(QColor("#f1c40f"))
        self.rubber_band_temp.setWidth(2)
        self.rubber_band_temp.setLineStyle(Qt.PenStyle.DashLine)

        # Deshacer con Backspace / Supr, aunque el foco este en el mapa
        for key in (Qt.Key.Key_Backspace, Qt.Key.Key_Delete):
            shortcut = QShortcut(QKeySequence(key), self)
            shortcut.setContext(Qt.ShortcutContext.ApplicationShortcut)
            shortcut.activated.connect(self.undo_last_point)

    # ------------------------------------------------------------------
    # DEM / preferencias
    # ------------------------------------------------------------------

    def on_layers_changed(self, *args):
        current_layer = self.combo_dem.currentData()
        self.load_rasters()
        if current_layer is not None:
            for i in range(self.combo_dem.count()):
                if self.combo_dem.itemData(i) == current_layer:
                    self.combo_dem.setCurrentIndex(i)
                    break

    def load_rasters(self):
        self.combo_dem.clear()
        for layer in QgsProject.instance().mapLayers().values():
            if isinstance(layer, QgsRasterLayer):
                self.combo_dem.addItem(layer.name(), layer)

    def load_preferences(self):
        last_dem = self.settings.value("last_dem", "")
        if last_dem:
            index = self.combo_dem.findText(last_dem)
            if index >= 0:
                self.combo_dem.setCurrentIndex(index)

        try:
            self.last_alpha_used = float(self.settings.value("last_alpha", DEFAULT_DIP_ANGLE))
        except (TypeError, ValueError):
            self.last_alpha_used = DEFAULT_DIP_ANGLE

    def save_preferences(self):
        self.settings.setValue("last_dem", self.combo_dem.currentText())
        self.settings.setValue("last_alpha", self.last_alpha_used)

    def check_crs_warning(self):
        canvas_crs = self.iface.mapCanvas().mapSettings().destinationCrs()
        self.is_geographic_crs = canvas_crs.isGeographic()
        self.update_status_pill()

    def update_status_pill(self, *args):
        """Muestra como maximo un aviso a la vez, con prioridad: espesor
        negativo > sin alturas > subhorizontal > CRS geografico. Evita que
        varios avisos se solapen o compitan por espacio."""
        if self.last_thickness_negative:
            text, style = "\u26a0\ufe0f Negative thickness", self.PILL_RED
        elif self.combo_elevation_source.currentData() == "none":
            text, style = "\u26a0\ufe0f Rough estimate, flat terrain considered", self.PILL_AMBER
        elif self.checkbox_subhorizontal.isChecked():
            text, style = "\u2139\ufe0f Elevation difference only", self.PILL_BLUE
        elif self.is_geographic_crs:
            text, style = "\u26a0\ufe0f Geographic CRS \u2014 distances still in meters", self.PILL_AMBER
        else:
            text, style = "", ""

        self.label_status_pill.setText(text)
        self.label_status_pill.setStyleSheet(style)
        self.label_status_pill.setVisible(bool(text))

    def on_elevation_source_changed(self, *args):
        """Ajusta la interfaz al modo de elevacion elegido: DEM, manual, o
        ninguna (estimacion en bruto). Cambiar de modo reinicia la medicion
        en curso para no mezclar fuentes de altura a medio camino."""
        mode = self.combo_elevation_source.currentData()

        show_dem = (mode == "dem")
        self.label_dem.setVisible(show_dem)
        self.combo_dem.setVisible(show_dem)

        # Sin alturas, el modo (sub)horizontal no tiene sentido (depende
        # por completo de la diferencia de cota): se desactiva y desmarca.
        no_elevation = (mode == "none")
        self.checkbox_subhorizontal.setDisabled(no_elevation)
        if no_elevation:
            self.checkbox_subhorizontal.setChecked(False)

        self.update_status_pill()

        if self.line_points or self.segments:
            self.reset_measurement()

    def get_or_ask_elevation(self, point, prompt_label):
        """Devuelve la elevacion de un punto segun el modo actual: leida
        del DEM, preguntada al usuario, o 0.0 en modo sin alturas. Devuelve
        None si el usuario cancela o falta algo (DEM no seleccionado)."""
        mode = self.combo_elevation_source.currentData()

        if mode == "none":
            return 0.0

        if mode == "manual":
            value, ok = QInputDialog.getDouble(
                self, "Elevation", prompt_label,
                self.last_manual_elevation, -500.0, 9000.0, 1
            )
            if not ok:
                return None
            self.last_manual_elevation = value
            return value

        # mode == "dem"
        dem_layer = self.combo_dem.currentData()
        if dem_layer is None:
            QMessageBox.warning(self, "Error", "Please select a DEM layer.")
            return None
        return self.get_height_from_dem(dem_layer, point)

    # ------------------------------------------------------------------
    # Manejo de clics / traza acumulada
    # ------------------------------------------------------------------

    def get_point(self, point, button):
        if button == Qt.MouseButton.LeftButton:
            self.add_point(point)
        elif button == Qt.MouseButton.RightButton:
            self.reset_measurement()

    def add_point(self, point):
        """Anade un punto a la traza. El primer punto de toda la traza solo
        registra su elevacion (no hay segmento que cerrar todavia). A partir
        del segundo, se pregunta primero su elevacion (top) y despues el
        buzamiento del segmento (salvo en modo subhorizontal)."""
        if not self.line_points:
            elevation = self.get_or_ask_elevation(point, "Base elevation (m):")
            if elevation is None:
                return  # cancelado, o falta DEM: no se registra el punto
            self.point_elevations.append(elevation)
            self.line_points.append(point)
            self.rubber_band_points.addPoint(point)
            self.rubber_band_points_halo.addPoint(point)
            self.draw_line()
            return

        last_point = self.line_points[-1]
        if point == last_point:
            QMessageBox.warning(self, "Error", "Please click a different point.")
            return

        elevation = self.get_or_ask_elevation(point, "Top elevation (m):")
        if elevation is None:
            return  # cancelado, o falta DEM: no se anade nada

        segment_number = len(self.segments) + 1
        subhorizontal = self.checkbox_subhorizontal.isChecked()

        if subhorizontal:
            # Sin pregunta de angulo: se fija a 0 grados, que es
            # exactamente el limite al que converge la formula general
            # (el resultado pasa a ser techo - base, sin depender de d).
            alpha = 0.0
        else:
            alpha, ok = QInputDialog.getDouble(
                self, f"Segment {segment_number}",
                "Dip of the strata (degrees):",
                self.last_alpha_used, 0.0, 89.9, 1
            )
            if not ok:
                return  # cancelado: no se anade ni el punto ni el segmento
            self.last_alpha_used = alpha

        ai = self.point_elevations[-1]
        af = elevation
        d = self.get_horizontal_distance_m(last_point, point)
        segment = self.compute_segment(ai, af, d, alpha)
        segment["start"] = last_point
        segment["end"] = point
        segment["subhorizontal"] = subhorizontal

        self.segments.append(segment)
        self.update_segments_ui()

        self.point_elevations.append(af)
        self.line_points.append(point)
        self.rubber_band_points.addPoint(point)
        self.rubber_band_points_halo.addPoint(point)
        self.draw_line()

    def draw_line(self):
        """Dibuja una línea por cada segmento normal ya cerrado. Los
        segmentos en modo subhorizontal no se conectan con una línea: la
        distancia entre esos dos puntos no interviene en el cálculo, así
        que dibujarla induciría a pensar que sí importa."""
        self.rubber_band_line.reset(QgsWkbTypes.GeometryType.LineGeometry)
        self.rubber_band_line_halo.reset(QgsWkbTypes.GeometryType.LineGeometry)
        parts = [[seg["start"], seg["end"]] for seg in self.segments if not seg.get("subhorizontal")]
        if parts:
            geom = QgsGeometry.fromMultiPolylineXY(parts)
            self.rubber_band_line.setToGeometry(geom, None)
            self.rubber_band_line_halo.setToGeometry(geom, None)

    def update_temp_line(self, point):
        """Línea discontinua de vista previa hasta el cursor. No se muestra
        en modo subhorizontal, donde el segundo clic puede ir en cualquier
        sitio y la dirección/distancia no tiene ningún significado."""
        if self.line_points and not self.checkbox_subhorizontal.isChecked():
            self.rubber_band_temp.reset()
            temp_line = QgsGeometry.fromPolylineXY([self.line_points[-1], point])
            self.rubber_band_temp.setToGeometry(temp_line, None)
        else:
            self.rubber_band_temp.reset()

    def undo_last_point(self):
        if not self.isVisible() or not self.line_points:
            return

        self.line_points.pop()
        if self.point_elevations:
            self.point_elevations.pop()

        self.rubber_band_points.reset(QgsWkbTypes.GeometryType.PointGeometry)
        self.rubber_band_points_halo.reset(QgsWkbTypes.GeometryType.PointGeometry)
        for p in self.line_points:
            self.rubber_band_points.addPoint(p)
            self.rubber_band_points_halo.addPoint(p)

        if self.segments:
            self.segments.pop()
            self.update_segments_ui()

        self.draw_line()
        self.rubber_band_temp.reset()
        self.refresh_last_segment_labels()

    def refresh_last_segment_labels(self):
        """Actualiza la línea de Base/Top/Distancia con el último segmento
        que quede tras un undo (o la deja vacía si no queda ninguno)."""
        if self.segments:
            last = self.segments[-1]
            no_elevation = (self.combo_elevation_source.currentData() == "none")
            self._set_segment_info_text(last["ai"], last["af"], last["d"],
                                         last.get("subhorizontal"), hide_elevations=no_elevation)
            self._apply_thickness_style(last["ST"])
        else:
            self.label_segment_info.setText("Base: -   Top: -   Distance: -")
            self.last_thickness_negative = False
            self.update_status_pill()

    def reset_measurement(self):
        self.line_points = []
        self.point_elevations = []
        self.segments = []
        self.label_segment_info.setText("Base: -   Top: -   Distance: -")
        self.last_thickness_negative = False
        self.update_status_pill()
        self.list_segments.clear()
        self.label_total.setText("TOTAL THICKNESS: -")
        self.rubber_band_line.reset(QgsWkbTypes.GeometryType.LineGeometry)
        self.rubber_band_line_halo.reset(QgsWkbTypes.GeometryType.LineGeometry)
        self.rubber_band_points.reset(QgsWkbTypes.GeometryType.PointGeometry)
        self.rubber_band_points_halo.reset(QgsWkbTypes.GeometryType.PointGeometry)
        self.rubber_band_temp.reset(QgsWkbTypes.GeometryType.LineGeometry)

    # ------------------------------------------------------------------
    # Cálculo
    # ------------------------------------------------------------------

    def _set_segment_info_text(self, ai, af, d, subhorizontal=False, hide_elevations=False):
        distance_text = "n/a" if subhorizontal else f"{d:.2f} m"
        if hide_elevations:
            self.label_segment_info.setText(f"Base: n/a   Top: n/a   Distance: {distance_text}")
        else:
            self.label_segment_info.setText(f"Base: {ai:.2f} m   Top: {af:.2f} m   Distance: {distance_text}")

    def _apply_thickness_style(self, ST):
        """Registra si el ultimo espesor calculado es negativo (senal de que
        el orden de los clics, base -> techo, esta invertido)."""
        self.last_thickness_negative = (ST < 0)
        self.update_status_pill()

    def compute_segment(self, ai, af, d, alpha):
        """Calcula el espesor de un segmento a partir de alturas y distancia
        ya resueltas (por el DEM, a mano, o 0.0 en modo sin alturas)."""
        alpha_rad = math.radians(alpha)
        delta_h = ai - af

        # ST = d * sin(alpha) - delta_h * cos(alpha)
        # En modo subhorizontal alpha=0, por lo que sin(0)=0 y el término
        # de distancia desaparece del todo: ST = af - ai, sin importar d.
        # En modo "sin alturas" ai=af=0, asi que solo queda ST = d*sin(alpha).
        ST = d * math.sin(alpha_rad) - delta_h * math.cos(alpha_rad)

        no_elevation = (self.combo_elevation_source.currentData() == "none")
        self._set_segment_info_text(ai, af, d, hide_elevations=no_elevation)
        self._apply_thickness_style(ST)

        return {"alpha": alpha, "ai": ai, "af": af, "d": d, "ST": ST}

    def edit_segment_dip(self, item):
        """Doble clic en un segmento de la lista: permite corregir su
        buzamiento sin tener que deshacer los segmentos posteriores."""
        index = self.list_segments.row(item)
        if index < 0 or index >= len(self.segments):
            return

        seg = self.segments[index]
        new_alpha, ok = QInputDialog.getDouble(
            self, f"Edit segment {index + 1}",
            "Dip of the strata (degrees):",
            seg["alpha"], 0.0, 89.9, 1
        )
        if not ok:
            return

        alpha_rad = math.radians(new_alpha)
        delta_h = seg["ai"] - seg["af"]
        seg["alpha"] = new_alpha
        seg["ST"] = seg["d"] * math.sin(alpha_rad) - delta_h * math.cos(alpha_rad)
        seg["subhorizontal"] = (new_alpha == 0.0)
        self.last_alpha_used = new_alpha

        self.update_segments_ui()
        self.draw_line()  # el segmento puede haber cambiado de subhorizontal a normal, o viceversa

        if index == len(self.segments) - 1:
            self.refresh_last_segment_labels()

    def update_segments_ui(self):
        self.list_segments.clear()
        total_ST = 0.0

        for i, seg in enumerate(self.segments, start=1):
            total_ST += seg["ST"]
            if seg.get("subhorizontal"):
                text = f"Seg {i}: subhorizontal   ST={seg['ST']:.2f} m"
            else:
                text = f"Seg {i}: \u03b1={seg['alpha']:.1f}\u00b0  d={seg['d']:.1f} m  ST={seg['ST']:.2f} m"
            item = QListWidgetItem(text)
            if seg["ST"] < 0:
                item.setForeground(QColor("#c0392b"))
            self.list_segments.addItem(item)

        self.label_total.setText(f"TOTAL THICKNESS: {total_ST:.2f} m")

    # ------------------------------------------------------------------
    # DEM / geometria auxiliares
    # ------------------------------------------------------------------

    def get_horizontal_distance_m(self, start_point, end_point):
        canvas_crs = self.iface.mapCanvas().mapSettings().destinationCrs()
        distance_area = QgsDistanceArea()
        distance_area.setSourceCrs(canvas_crs, QgsProject.instance().transformContext())
        distance_area.setEllipsoid(QgsProject.instance().ellipsoid())
        return distance_area.measureLine(start_point, end_point)

    def get_height_from_dem(self, dem_layer, point):
        canvas_crs = self.iface.mapCanvas().mapSettings().destinationCrs()
        dem_crs = dem_layer.crs()

        point_in_dem_crs = point
        if canvas_crs != dem_crs:
            try:
                transform = QgsCoordinateTransform(canvas_crs, dem_crs, QgsProject.instance())
                point_in_dem_crs = transform.transform(point)
            except Exception:
                QMessageBox.warning(
                    self, "Error",
                    "Could not reproject the point to the DEM's CRS. "
                    "Check that the DEM has a valid CRS defined."
                )
                return None

        ident = dem_layer.dataProvider().identify(point_in_dem_crs, QgsRaster.IdentifyFormat.IdentifyFormatValue)
        if ident.isValid():
            height = ident.results().get(1)  # banda 1
            if height is not None:
                return float(height)

        QMessageBox.warning(
            self, "Error",
            f"Could not retrieve height for point {point}. "
            "It may fall outside the DEM extent or on a NoData cell."
        )
        return None

    def closeEvent(self, event):
        self.save_preferences()
        self.rubber_band_line.reset()
        self.rubber_band_line_halo.reset()
        self.rubber_band_points.reset()
        self.rubber_band_points_halo.reset()
        self.rubber_band_temp.reset()
        self.iface.mapCanvas().xyCoordinates.disconnect(self.update_temp_line)
        self.iface.mapCanvas().unsetMapTool(self.map_tool)
        try:
            self.iface.mapCanvas().destinationCrsChanged.disconnect(self.check_crs_warning)
            QgsProject.instance().layersAdded.disconnect(self.on_layers_changed)
            QgsProject.instance().layersRemoved.disconnect(self.on_layers_changed)
        except TypeError:
            pass
        super().closeEvent(event)


class StratigraphicThickness:
    """Clase principal del plugin (registro en el menu/barra de QGIS)."""

    def __init__(self, iface):
        self.iface = iface
        self.dialog = None

    def initGui(self):
        self.action = QAction(
            QIcon(":/plugins/stratigraphic_thickness/icon.png"),
            "Stratigraphic Thickness",
            self.iface.mainWindow()
        )
        self.action.triggered.connect(self.run)
        self.iface.addToolBarIcon(self.action)
        self.iface.addPluginToMenu("&Stratigraphic Thickness", self.action)

    def unload(self):
        self.iface.removeToolBarIcon(self.action)
        self.iface.removePluginMenu("&Stratigraphic Thickness", self.action)

    def run(self):
        self.dialog = StratigraphicThicknessDialog(self.iface)
        self.dialog.show()
        self.iface.mapCanvas().setMapTool(self.dialog.map_tool)
