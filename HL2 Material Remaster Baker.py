bl_info = {
    "name": "HL2 Material Remaster Baker",
    "author": "Jonatan Mercado",
    "version": (0, 6, 10),
    "blender": (4, 0, 0),
    "location": "View3D / Image Editor > Sidebar > HL2 Remaster",
    "description": "Orthographic PBR remaster setup, baker, tester, and UV tools for HL2 material textures.",
    "category": "Material",
}

import bpy
import os
import math
import array
import bmesh
import json
import urllib.request
from math import hypot
from collections import defaultdict
from bpy.props import CollectionProperty, StringProperty
from bpy_extras.io_utils import ImportHelper


ADDON_COLLECTION_NAME = "HL2_Remaster_Setup"
PLANE_NAME = "HL2_Remaster_Plane"
CAMERA_NAME = "HL2_Remaster_Camera"
MATERIAL_NAME = "HL2_Remaster_Material"

TEST_PLANE_NAME = "HL2_Test_Maps_Plane"
TEST_MATERIAL_PREFIX = "HL2_Test_"
OLD_MAP_PLANE_NAME = "HL2_Old_Map_Plane"
OLD_MAP_MATERIAL_NAME = "HL2_Old_Map_Material"

MODE_FRONT = "FRONT"
MODE_TOP_DOWN = "TOP_DOWN"

VERTICAL_PLANE_ROTATION = (math.radians(90.0), 0.0, 0.0)
TOP_DOWN_PLANE_ROTATION = (0.0, 0.0, 0.0)
FRONT_CAMERA_ROTATION = (math.radians(90.0), 0.0, 0.0)
TOP_DOWN_CAMERA_ROTATION = (0.0, 0.0, 0.0)

UV_RECTIFY_PRECISION = 3

UPDATE_VERSION_URL = "https://raw.githubusercontent.com/Jota3D-creator/HL2-Material-Remaster-Baker/main/version.json"


# -----------------------------------------------------------------------------
# General helpers
# -----------------------------------------------------------------------------

def ensure_output_folder(path):
    if path and not os.path.exists(path):
        os.makedirs(path, exist_ok=True)


def out_path(folder, tex_name, suffix, ext="png"):
    folder = bpy.path.abspath(folder)
    ensure_output_folder(folder)
    return os.path.join(folder, f"{tex_name}_{suffix}.{ext}")


def purge_orphan_data():
    try:
        for _ in range(3):
            bpy.ops.outliner.orphans_purge(
                do_local_ids=True,
                do_linked_ids=True,
                do_recursive=True,
            )
    except Exception:
        pass


def clear_scene_objects():
    bpy.ops.object.select_all(action='SELECT')
    bpy.ops.object.delete()

    col = bpy.data.collections.get(ADDON_COLLECTION_NAME)
    if col and len(col.objects) == 0:
        try:
            bpy.data.collections.remove(col)
        except Exception:
            pass

    purge_orphan_data()


def ensure_collection(name):
    scene = bpy.context.scene
    col = bpy.data.collections.get(name)

    if col is None:
        col = bpy.data.collections.new(name)

    if col.name not in [child.name for child in scene.collection.children]:
        try:
            scene.collection.children.link(col)
        except Exception:
            pass

    return col


def move_to_collection(obj, col):
    try:
        for old in list(obj.users_collection):
            old.objects.unlink(obj)
        col.objects.link(obj)
    except Exception:
        pass


def set_image_color_space(img, color_space):
    try:
        img.colorspace_settings.name = color_space
    except Exception:
        pass


def load_image_safe(path, color_space="sRGB"):
    if not path:
        return None

    path = bpy.path.abspath(path)

    if not os.path.isfile(path):
        return None

    try:
        img = bpy.data.images.load(path, check_existing=True)
        set_image_color_space(img, color_space)
        return img
    except Exception:
        return None


def clear_viewer_images():
    for img in list(bpy.data.images):
        if img.name.startswith("Viewer Node") or img.name.startswith("HL2_Viewer_"):
            try:
                bpy.data.images.remove(img)
            except Exception:
                pass


def viewer_images():
    return [
        img for img in bpy.data.images
        if img.name.startswith("Viewer Node") or img.name.startswith("HL2_Viewer_")
    ]


def rename_latest_viewer(pass_name):
    imgs = viewer_images()

    if not imgs:
        return None

    img = imgs[-1]
    img.name = f"HL2_Viewer_{pass_name}"
    return img


def save_image_to_path(img, path):
    if img is None:
        return False, "Viewer image not found"

    try:
        img.filepath_raw = path
        img.file_format = 'PNG'
        img.save()
        return True, path
    except Exception:
        pass

    try:
        img.save_render(path)
        return True, path
    except Exception as error:
        return False, f"Could not save image: {error}"


def save_viewer_as_exr(scene, img, path):
    if img is None:
        return False, "Viewer image not found"

    ensure_output_folder(os.path.dirname(path))
    settings = save_render_settings(scene)

    try:
        scene.render.image_settings.file_format = 'OPEN_EXR'
        scene.render.image_settings.color_mode = 'BW'
        scene.render.image_settings.color_depth = '32'

        try:
            scene.render.image_settings.exr_codec = 'ZIP'
        except Exception:
            pass

        img.save_render(path, scene=scene)
        restore_render_settings(scene, settings)

        if os.path.exists(path):
            return True, path

        return False, f"EXR was not created: {path}"

    except Exception as error:
        restore_render_settings(scene, settings)
        return False, f"Could not save displacement EXR: {error}"


def show_image_in_image_editor(img):
    if img is None:
        return

    try:
        for area in bpy.context.window.screen.areas:
            if area.type == 'IMAGE_EDITOR':
                area.spaces.active.image = img
                return
    except Exception:
        pass


def find_exported_texture(output_folder, texture_name, suffix, extensions):
    output_folder = bpy.path.abspath(output_folder)

    for ext in extensions:
        path = os.path.join(output_folder, f"{texture_name}_{suffix}.{ext}")

        if os.path.isfile(path):
            return path

    return ""


def clamp01(value):
    return max(0.0, min(1.0, value))


def safe_texture_name(value):
    name = str(value or "").strip()
    if not name:
        return "HL2_Remaster_Material"
    invalid = '<>:"/\\|?*'
    for char in invalid:
        name = name.replace(char, "_")
    return name


def suggested_blend_path(props):
    folder = bpy.path.abspath(props.working_blend_folder)
    if not folder:
        return ""
    ensure_output_folder(folder)
    return os.path.join(folder, f"{safe_texture_name(props.texture_name)}.blend")


def save_blend_for_texture(props):
    if not props.save_blend_before_setup:
        return True, "Save before setup disabled"
    if not props.working_blend_folder:
        return False, "Please set a Working Blend Folder before creating the setup"
    path = suggested_blend_path(props)
    if not path:
        return False, "Could not build blend save path"
    try:
        bpy.ops.wm.save_as_mainfile(filepath=path)
        return True, f"Blend saved: {path}"
    except Exception as error:
        return False, f"Could not save blend file: {error}"


def get_working_material(props=None):
    if props is not None:
        tex_mat = bpy.data.materials.get(safe_texture_name(props.texture_name))
        if tex_mat:
            return tex_mat
    plane = bpy.data.objects.get(PLANE_NAME)
    if plane and plane.type == 'MESH':
        try:
            if plane.active_material:
                return plane.active_material
        except Exception:
            pass
        try:
            if plane.material_slots and plane.material_slots[0].material:
                return plane.material_slots[0].material
        except Exception:
            pass
    legacy = bpy.data.materials.get(MATERIAL_NAME)
    if legacy:
        return legacy
    return None


def update_old_base_image_in_material(mat, source_texture_path):
    if mat is None or not mat.use_nodes:
        return False
    img = load_image_safe(source_texture_path, "sRGB")
    if img is None:
        return False
    nodes = mat.node_tree.nodes
    for node_name in ["OLD_BaseColor", "HL2_Original_BaseColor_Disconnected", "OLD_Map_BaseColor"]:
        node = nodes.get(node_name)
        if node and node.bl_idname == "ShaderNodeTexImage":
            node.image = img
            set_image_color_space(img, "sRGB")
            try:
                node.interpolation = 'Smart'
            except Exception:
                pass
    return True


def rename_working_material_to_texture(mat, props):
    if mat is None:
        return None
    mat.name = safe_texture_name(props.texture_name)
    return mat


def displacement_scale_records():
    records = []
    for mat in bpy.data.materials:
        if not mat or not mat.use_nodes:
            continue
        for node in mat.node_tree.nodes:
            if node.bl_idname == "ShaderNodeDisplacement" and "Scale" in node.inputs:
                try:
                    records.append((node, node.inputs["Scale"].default_value))
                except Exception:
                    pass
    return records


def set_displacement_scales(records, value):
    for node, old_value in records:
        try:
            node.inputs["Scale"].default_value = value
        except Exception:
            pass


def restore_displacement_scales(records):
    for node, old_value in records:
        try:
            node.inputs["Scale"].default_value = old_value
        except Exception:
            pass


def get_material_output_node(mat):
    if not mat or not mat.use_nodes:
        return None

    named = mat.node_tree.nodes.get("Material Output")

    if named and named.bl_idname == "ShaderNodeOutputMaterial":
        return named

    for node in mat.node_tree.nodes:
        if node.bl_idname == "ShaderNodeOutputMaterial":
            return node

    return None


def get_displacement_node(mat):
    if not mat or not mat.use_nodes:
        return None

    named = mat.node_tree.nodes.get("HL2_Displacement")

    if named and named.bl_idname == "ShaderNodeDisplacement":
        return named

    for node in mat.node_tree.nodes:
        if node.bl_idname == "ShaderNodeDisplacement":
            return node

    return None


def disconnect_material_displacement(mat):
    if not mat or not mat.use_nodes:
        return False

    output = get_material_output_node(mat)

    if output is None or "Displacement" not in output.inputs:
        return False

    links = mat.node_tree.links

    for link in list(output.inputs["Displacement"].links):
        links.remove(link)

    return True


def connect_material_displacement(mat):
    if not mat or not mat.use_nodes:
        return False

    output = get_material_output_node(mat)
    displacement = get_displacement_node(mat)

    if output is None or displacement is None:
        return False

    if "Displacement" not in output.inputs or "Displacement" not in displacement.outputs:
        return False

    links = mat.node_tree.links

    for link in list(output.inputs["Displacement"].links):
        links.remove(link)

    links.new(displacement.outputs["Displacement"], output.inputs["Displacement"])
    return True


def set_camera_background_opacity(scene, opacity):
    cam = scene.camera or bpy.data.objects.get(CAMERA_NAME)

    if cam is None or cam.type != 'CAMERA':
        return False

    try:
        cam.data.show_background_images = True

        for bg in cam.data.background_images:
            bg.alpha = float(opacity)
            bg.display_depth = 'FRONT'

        return True
    except Exception:
        return False



# -----------------------------------------------------------------------------
# Addon Update helpers
# -----------------------------------------------------------------------------

def get_local_addon_version_tuple():
    version = bl_info.get("version", (0, 0, 0))

    try:
        return tuple(int(v) for v in version)
    except Exception:
        return (0, 0, 0)


def version_tuple_to_string(version_tuple):
    try:
        return ".".join(str(int(v)) for v in version_tuple)
    except Exception:
        return "0.0.0"


def parse_version_string(value):
    parts = str(value or "0.0.0").strip().split(".")
    parsed = []

    for part in parts:
        try:
            parsed.append(int(part))
        except Exception:
            parsed.append(0)

    while len(parsed) < 3:
        parsed.append(0)

    return tuple(parsed[:3])


def get_local_addon_version_string():
    return version_tuple_to_string(get_local_addon_version_tuple())


def is_remote_version_newer(local_version, remote_version):
    return parse_version_string(remote_version) > parse_version_string(local_version)


def get_addon_install_path():
    try:
        return os.path.abspath(__file__)
    except Exception:
        return ""


def get_update_json_url(props=None):
    if props is not None:
        url = str(getattr(props, "addon_update_json_url", "") or "").strip()

        if url:
            return url

    return UPDATE_VERSION_URL


def fetch_update_json(props=None, timeout=20):
    try:
        request = urllib.request.Request(
            get_update_json_url(props),
            headers={"User-Agent": "Mozilla/5.0"},
        )

        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8")

        data = json.loads(raw)

        if not isinstance(data, dict):
            return False, "Update JSON is not a valid object", None

        if "version" not in data or "download_url" not in data:
            return False, "Update JSON must include version and download_url", None

        return True, "Update JSON loaded", data

    except Exception as error:
        return False, f"Could not check updates: {error}", None


def download_text_file(url, timeout=60):
    try:
        request = urllib.request.Request(
            url,
            headers={"User-Agent": "Mozilla/5.0"},
        )

        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()

        return True, "Download complete", raw

    except Exception as error:
        return False, f"Could not download update: {error}", None


def install_addon_update_from_url(download_url):
    addon_path = get_addon_install_path()

    if not addon_path:
        return False, "Could not determine current addon path"

    if not os.path.isfile(addon_path):
        return False, f"Addon file does not exist: {addon_path}"

    ok, message, raw = download_text_file(download_url)

    if not ok:
        return False, message

    try:
        text = raw.decode("utf-8")
    except Exception:
        return False, "Downloaded file is not valid UTF-8 text"

    if "bl_info" not in text or "HL2 Material Remaster Baker" not in text:
        return False, "Downloaded file does not look like the HL2 addon"

    backup_path = addon_path + ".backup"

    try:
        with open(addon_path, "r", encoding="utf-8") as current_file:
            current_text = current_file.read()

        with open(backup_path, "w", encoding="utf-8") as backup_file:
            backup_file.write(current_text)

        with open(addon_path, "w", encoding="utf-8") as addon_file:
            addon_file.write(text)

        return True, "Update installed. Please restart Blender."

    except Exception as error:
        return False, f"Could not install update: {error}"


def update_addon_update_props(props, data):
    local_version = get_local_addon_version_string()
    remote_version = str(data.get("version", "")).strip()
    changelog = data.get("changelog", [])

    if isinstance(changelog, list):
        changelog_text = "\n".join(str(item) for item in changelog)
    else:
        changelog_text = str(changelog or "")

    props.addon_local_version = local_version
    props.addon_available_version = remote_version
    props.addon_download_url = str(data.get("download_url", ""))
    props.addon_update_changelog = changelog_text

    if is_remote_version_newer(local_version, remote_version):
        props.addon_update_status = f"Update available: {remote_version}"
        props.addon_update_available = True
    else:
        props.addon_update_status = f"Up to date: {local_version}"
        props.addon_update_available = False



def remove_camera_backgrounds(camera):
    if camera is None or not hasattr(camera.data, "background_images"):
        return

    try:
        while camera.data.background_images:
            camera.data.background_images.remove(camera.data.background_images[0])
    except Exception:
        pass


def set_camera_background_from_source(scene, props):
    cam = scene.camera or bpy.data.objects.get(CAMERA_NAME)

    if cam is None or cam.type != 'CAMERA':
        return False, "Camera not found"

    img = load_image_safe(props.source_texture, "sRGB")

    if img is None:
        return False, "Source Base Color image not found"

    remove_camera_backgrounds(cam)

    try:
        bg = cam.data.background_images.new()
        bg.image = img
        bg.display_depth = 'FRONT'
        bg.alpha = float(props.camera_bg_opacity)
        bg.frame_method = 'FIT'
        cam.data.show_background_images = True
        return True, "Camera background updated"
    except Exception as error:
        return False, str(error)


# -----------------------------------------------------------------------------
# Render settings
# -----------------------------------------------------------------------------

def setup_gpu_if_available():
    try:
        addon = bpy.context.preferences.addons.get("cycles")

        if not addon:
            return None

        prefs = addon.preferences

        for dev_type in ["OPTIX", "CUDA", "HIP", "METAL", "ONEAPI"]:
            try:
                prefs.compute_device_type = dev_type
                prefs.get_devices()

                used = False

                for dev in prefs.devices:
                    if dev.type != 'CPU':
                        dev.use = True
                        used = True
                    else:
                        dev.use = False

                if used:
                    return dev_type

            except Exception:
                pass

    except Exception:
        pass

    return None


def set_cycles_dicing(scene):
    cyc = getattr(scene, "cycles", None)

    if not cyc:
        return

    vals = {
        "dicing_rate": 1.0,
        "render_dicing_rate": 1.0,
        "preview_dicing_rate": 1.0,
        "viewport_dicing_rate": 1.0,
        "offscreen_dicing_scale": 4.0,
        "max_subdivisions": 12,
    }

    for key, value in vals.items():
        try:
            if hasattr(cyc, key):
                setattr(cyc, key, value)
        except Exception:
            pass


def apply_resolution(scene, resolution):
    resolution = int(resolution)
    scene.render.resolution_x = resolution
    scene.render.resolution_y = resolution
    scene.render.resolution_percentage = 100


def setup_scene(scene, props, preview=True):
    scene.render.engine = 'CYCLES'

    try:
        scene.cycles.device = 'GPU' if props.use_gpu else 'CPU'
    except Exception:
        pass

    if props.use_gpu:
        setup_gpu_if_available()

    set_cycles_dicing(scene)
    apply_resolution(scene, props.resolution)

    scene.render.film_transparent = props.transparent_bg

    try:
        scene.cycles.samples = 128
        scene.cycles.use_denoising = True
    except Exception:
        pass

    try:
        scene.display_settings.display_device = 'sRGB'
        scene.view_settings.view_transform = 'AgX' if preview else 'Standard'
        scene.view_settings.look = 'Medium High Contrast' if preview else 'None'
        scene.view_settings.exposure = 0.0
        scene.view_settings.gamma = 1.0
    except Exception:
        pass

    if scene.world is None:
        scene.world = bpy.data.worlds.new("World")

    scene.world.use_nodes = True
    bg = scene.world.node_tree.nodes.get("Background")

    if bg:
        bg.inputs[0].default_value = (1.0, 1.0, 1.0, 1.0)
        bg.inputs[1].default_value = 1.0


def save_render_settings(scene):
    return {
        "filepath": scene.render.filepath,
        "file_format": scene.render.image_settings.file_format,
        "color_mode": scene.render.image_settings.color_mode,
        "color_depth": scene.render.image_settings.color_depth,
        "view_transform": scene.view_settings.view_transform,
        "look": scene.view_settings.look,
        "exposure": scene.view_settings.exposure,
        "gamma": scene.view_settings.gamma,
        "use_nodes": scene.use_nodes,
        "use_compositing": scene.render.use_compositing,
        "resolution_x": scene.render.resolution_x,
        "resolution_y": scene.render.resolution_y,
        "resolution_percentage": scene.render.resolution_percentage,
    }


def restore_render_settings(scene, settings):
    try:
        scene.render.filepath = settings["filepath"]
        scene.render.image_settings.file_format = settings["file_format"]
        scene.render.image_settings.color_mode = settings["color_mode"]
        scene.render.image_settings.color_depth = settings["color_depth"]
        scene.view_settings.view_transform = settings["view_transform"]
        scene.view_settings.look = settings["look"]
        scene.view_settings.exposure = settings["exposure"]
        scene.view_settings.gamma = settings["gamma"]
        scene.use_nodes = settings["use_nodes"]
        scene.render.use_compositing = settings["use_compositing"]
        scene.render.resolution_x = settings["resolution_x"]
        scene.render.resolution_y = settings["resolution_y"]
        scene.render.resolution_percentage = settings["resolution_percentage"]
    except Exception:
        pass


# -----------------------------------------------------------------------------
# Displacement and adaptive subdivision
# -----------------------------------------------------------------------------

def safe_set_material_displacement(mat):
    if mat is None:
        return

    try:
        if hasattr(mat, "displacement_method"):
            mat.displacement_method = 'DISPLACEMENT'
            return
    except Exception:
        pass

    try:
        if hasattr(mat, "cycles") and hasattr(mat.cycles, "displacement_method"):
            mat.cycles.displacement_method = 'DISPLACEMENT'
    except Exception:
        pass


def apply_displacement_only_to_all_materials():
    for mat in bpy.data.materials:
        safe_set_material_displacement(mat)


def safe_set_adaptive_subdivision(obj, subsurf):
    try:
        if hasattr(obj, "cycles") and hasattr(obj.cycles, "use_adaptive_subdivision"):
            obj.cycles.use_adaptive_subdivision = True
    except Exception:
        pass

    for prop_name in ["use_adaptive_subdivision", "adaptive_subdivision"]:
        try:
            if hasattr(subsurf, prop_name):
                setattr(subsurf, prop_name, True)
        except Exception:
            pass

    for prop_name in ["adaptive_subdivision_type", "subdivision_type_adaptive"]:
        try:
            if hasattr(subsurf, prop_name):
                setattr(subsurf, prop_name, 'PIXEL')
        except Exception:
            pass

    for prop_name in ["dicing_rate", "render_dicing_rate"]:
        try:
            if hasattr(subsurf, prop_name):
                setattr(subsurf, prop_name, 1.0)
        except Exception:
            pass

    for prop_name in ["preview_dicing_rate", "viewport_dicing_rate"]:
        try:
            if hasattr(subsurf, prop_name):
                setattr(subsurf, prop_name, 1.0)
        except Exception:
            pass


# -----------------------------------------------------------------------------
# View helpers
# -----------------------------------------------------------------------------

def set_view_to_camera_rendered(cam=None):
    if cam:
        bpy.context.scene.camera = cam

    try:
        for area in bpy.context.window.screen.areas:
            if area.type != 'VIEW_3D':
                continue

            space = area.spaces.active
            space.shading.type = 'RENDERED'
            region = next((r for r in area.regions if r.type == 'WINDOW'), None)

            if region:
                with bpy.context.temp_override(
                    window=bpy.context.window,
                    screen=bpy.context.screen,
                    area=area,
                    region=region,
                    space_data=space,
                    scene=bpy.context.scene,
                ):
                    bpy.ops.view3d.view_camera()

    except Exception:
        pass


def switch_to_workspace(name):
    try:
        for ws in bpy.data.workspaces:
            if ws.name.lower() == name.lower():
                bpy.context.window.workspace = ws
                break
    except Exception:
        pass


def switch_to_shading_and_camera(cam=None):
    switch_to_workspace("Shading")
    set_view_to_camera_rendered(cam)


def switch_to_compositing():
    switch_to_workspace("Compositing")


# -----------------------------------------------------------------------------
# Setup orientation
# -----------------------------------------------------------------------------

def plane_rotation_for_mode(mode):
    return VERTICAL_PLANE_ROTATION if mode == MODE_FRONT else TOP_DOWN_PLANE_ROTATION


def camera_rotation_for_mode(mode):
    return FRONT_CAMERA_ROTATION if mode == MODE_FRONT else TOP_DOWN_CAMERA_ROTATION


def camera_location_for_mode(mode, distance):
    if mode == MODE_FRONT:
        return (0.0, -distance, 0.0)

    return (0.0, 0.0, distance)


# -----------------------------------------------------------------------------
# Scene setup objects
# -----------------------------------------------------------------------------

def get_or_create_plane(col, size=2.0, mode=MODE_FRONT):
    rotation = plane_rotation_for_mode(mode)
    plane = bpy.data.objects.get(PLANE_NAME)

    if plane is None:
        bpy.ops.mesh.primitive_plane_add(size=size, location=(0.0, 0.0, 0.0), rotation=rotation)
        plane = bpy.context.object
        plane.name = PLANE_NAME
    else:
        plane.location = (0.0, 0.0, 0.0)
        plane.rotation_euler = rotation
        plane.scale = (1.0, 1.0, 1.0)

    move_to_collection(plane, col)
    plane.dimensions = (size, size, 0.0)

    bpy.ops.object.select_all(action='DESELECT')
    bpy.context.view_layer.objects.active = plane
    plane.select_set(True)

    try:
        bpy.ops.object.transform_apply(location=False, rotation=False, scale=True)
    except Exception:
        pass

    plane.rotation_euler = rotation

    subsurf = plane.modifiers.get("HL2_Subdivision") or plane.modifiers.new("HL2_Subdivision", 'SUBSURF')

    try:
        subsurf.subdivision_type = 'SIMPLE'
        subsurf.levels = 3
        subsurf.render_levels = 3
        subsurf.show_on_cage = True
    except Exception:
        pass

    safe_set_adaptive_subdivision(plane, subsurf)
    return plane


def get_or_create_camera(col, plane_size=2.0, camera_distance=3.0, mode=MODE_FRONT):
    cam = bpy.data.objects.get(CAMERA_NAME)

    if cam is None:
        data = bpy.data.cameras.new(CAMERA_NAME)
        cam = bpy.data.objects.new(CAMERA_NAME, data)
        col.objects.link(cam)
    else:
        move_to_collection(cam, col)

    cam.location = camera_location_for_mode(mode, camera_distance)
    cam.rotation_euler = camera_rotation_for_mode(mode)

    cam.data.type = 'ORTHO'
    cam.data.ortho_scale = plane_size
    cam.data.clip_start = 0.01
    cam.data.clip_end = 1000.0

    return cam


# -----------------------------------------------------------------------------
# Material template and Fill Principled
# -----------------------------------------------------------------------------

def create_material_template(source_texture_path="", texture_name=""):
    material_name = safe_texture_name(texture_name) if texture_name else MATERIAL_NAME
    mat = bpy.data.materials.get(material_name) or bpy.data.materials.new(material_name)
    mat.use_nodes = True
    safe_set_material_displacement(mat)

    nodes = mat.node_tree.nodes
    links = mat.node_tree.links
    nodes.clear()

    output = nodes.new("ShaderNodeOutputMaterial")
    output.name = "Material Output"
    output.location = (1150, 0)

    mix = nodes.new("ShaderNodeMixShader")
    mix.name = "HL2_OLD_NEW_SWITCH"
    mix.label = "OLD / NEW Preview Switch"
    mix.location = (900, 0)
    mix.inputs[0].default_value = 0.0

    old_diff = nodes.new("ShaderNodeBsdfDiffuse")
    old_diff.name = "OLD_DiffusePreview"
    old_diff.label = "OLD Diffuse Preview"
    old_diff.location = (600, 220)

    princ = nodes.new("ShaderNodeBsdfPrincipled")
    princ.name = "NEW_PBR_Principled"
    princ.label = "NEW PBR Principled"
    princ.location = (600, -130)

    if "Roughness" in princ.inputs:
        princ.inputs["Roughness"].default_value = 0.6

    if "Metallic" in princ.inputs:
        princ.inputs["Metallic"].default_value = 0.0

    texcoord = nodes.new("ShaderNodeTexCoord")
    texcoord.location = (-1450, 0)

    mapping = nodes.new("ShaderNodeMapping")
    mapping.location = (-1200, 0)

    links.new(texcoord.outputs["UV"], mapping.inputs["Vector"])

    old_base = nodes.new("ShaderNodeTexImage")
    old_base.name = "OLD_BaseColor"
    old_base.label = "OLD Base Color Reference"
    old_base.location = (-900, 250)
    old_base.interpolation = 'Smart'

    img = load_image_safe(source_texture_path, "sRGB")

    if img:
        old_base.image = img

    new_base = nodes.new("ShaderNodeTexImage")
    new_base.name = "NEW_BaseColor"
    new_base.label = "NEW Base Color"
    new_base.location = (-900, 0)

    new_rough = nodes.new("ShaderNodeTexImage")
    new_rough.name = "NEW_Roughness"
    new_rough.label = "NEW Roughness"
    new_rough.location = (-900, -220)

    new_met = nodes.new("ShaderNodeTexImage")
    new_met.name = "NEW_Metallic"
    new_met.label = "NEW Metallic"
    new_met.location = (-900, -420)

    new_norm = nodes.new("ShaderNodeTexImage")
    new_norm.name = "NEW_Normal"
    new_norm.label = "NEW Normal"
    new_norm.location = (-900, -650)

    normal_map = nodes.new("ShaderNodeNormalMap")
    normal_map.name = "HL2_NormalMap"
    normal_map.location = (-500, -650)

    new_h = nodes.new("ShaderNodeTexImage")
    new_h.name = "NEW_Height"
    new_h.label = "NEW Height / Displacement"
    new_h.location = (-900, -930)

    disp = nodes.new("ShaderNodeDisplacement")
    disp.name = "HL2_Displacement"
    disp.location = (-500, -930)

    if "Scale" in disp.inputs:
        disp.inputs["Scale"].default_value = 0.05

    if "Midlevel" in disp.inputs:
        disp.inputs["Midlevel"].default_value = 0.5

    rough_val = nodes.new("ShaderNodeValue")
    rough_val.name = "HL2_Roughness_Value"
    rough_val.outputs[0].default_value = 0.6
    rough_val.location = (-500, -190)

    met_val = nodes.new("ShaderNodeValue")
    met_val.name = "HL2_Metallic_Value"
    met_val.outputs[0].default_value = 0.0
    met_val.location = (-500, -390)

    for node in [new_base, new_rough, new_met, new_norm, new_h]:
        node.interpolation = 'Smart'
        links.new(mapping.outputs["Vector"], node.inputs["Vector"])

    links.new(old_base.outputs["Color"], old_diff.inputs["Color"])
    links.new(new_base.outputs["Color"], princ.inputs["Base Color"])

    if "Roughness" in princ.inputs:
        links.new(rough_val.outputs["Value"], princ.inputs["Roughness"])

    if "Metallic" in princ.inputs:
        links.new(met_val.outputs["Value"], princ.inputs["Metallic"])

    links.new(new_norm.outputs["Color"], normal_map.inputs["Color"])

    if "Normal" in princ.inputs:
        links.new(normal_map.outputs["Normal"], princ.inputs["Normal"])

    links.new(new_h.outputs["Color"], disp.inputs["Height"])
    links.new(disp.outputs["Displacement"], output.inputs["Displacement"])

    links.new(old_diff.outputs["BSDF"], mix.inputs[1])
    links.new(princ.outputs["BSDF"], mix.inputs[2])
    links.new(mix.outputs["Shader"], output.inputs["Surface"])

    return mat


def assign_material(obj, mat):
    if obj.data.materials:
        obj.data.materials[0] = mat
    else:
        obj.data.materials.append(mat)


def set_old_new_switch(value):
    mat = get_working_material(bpy.context.scene.hl2remaster_props)

    if not mat or not mat.use_nodes:
        return False, "Material not found"

    sw = mat.node_tree.nodes.get("HL2_OLD_NEW_SWITCH")

    if not sw:
        return False, "OLD / NEW switch not found"

    sw.inputs[0].default_value = value

    if value <= 0.0:
        disconnect_material_displacement(mat)
    else:
        connect_material_displacement(mat)

    return True, "Switch updated"


def get_or_create_node(mat, name, node_type, label=None, location=(0, 0)):
    node = mat.node_tree.nodes.get(name)

    if node is None:
        node = mat.node_tree.nodes.new(node_type)
        node.name = name
        node.location = location

    if label:
        node.label = label

    return node


def remove_links_to_socket(mat, socket):
    for link in list(socket.links):
        mat.node_tree.links.remove(link)


def connect_unique(mat, source, target):
    remove_links_to_socket(mat, target)
    mat.node_tree.links.new(source, target)


def detect_pbr_type(filename):
    name = filename.lower()

    if any(k in name for k in ["normal", "_nrm", "-nrm", "_nor", "-nor", "norm"]):
        return "normal"

    if any(k in name for k in ["height", "displacement", "disp", "_displ", "-displ", "_hgt", "-hgt"]):
        return "height"

    if any(k in name for k in ["roughness", "rough", "_rgh", "-rgh"]):
        return "roughness"

    if any(k in name for k in ["metallic", "metalness", "metal", "_met", "-met"]):
        return "metallic"

    if any(k in name for k in ["basecolor", "base_color", "basecolour", "albedo", "diffuse", "diff", "color", "colour", "_col", "-col"]):
        return "basecolor"

    return None


def assign_image_to_node(node, path, color_space):
    img = load_image_safe(path, color_space)

    if img is None:
        return False

    node.image = img

    try:
        node.interpolation = 'Smart'
    except Exception:
        pass

    return True


def fill_principled_from_files(props, filepaths):
    mat = get_working_material(props) or create_material_template(props.source_texture, props.texture_name)
    mat.use_nodes = True
    safe_set_material_displacement(mat)

    nodes = mat.node_tree.nodes

    princ = nodes.get("NEW_PBR_Principled") or get_or_create_node(
        mat,
        "NEW_PBR_Principled",
        "ShaderNodeBsdfPrincipled",
        "NEW PBR Principled",
        (600, -130),
    )

    out = nodes.get("Material Output") or get_or_create_node(
        mat,
        "Material Output",
        "ShaderNodeOutputMaterial",
        "Material Output",
        (1150, 0),
    )

    new_base = get_or_create_node(mat, "NEW_BaseColor", "ShaderNodeTexImage", "NEW Base Color", (-900, 0))
    new_rough = get_or_create_node(mat, "NEW_Roughness", "ShaderNodeTexImage", "NEW Roughness", (-900, -220))
    new_met = get_or_create_node(mat, "NEW_Metallic", "ShaderNodeTexImage", "NEW Metallic", (-900, -420))
    new_norm = get_or_create_node(mat, "NEW_Normal", "ShaderNodeTexImage", "NEW Normal", (-900, -650))
    norm_map = get_or_create_node(mat, "HL2_NormalMap", "ShaderNodeNormalMap", "Normal Map", (-500, -650))
    new_h = get_or_create_node(mat, "NEW_Height", "ShaderNodeTexImage", "NEW Height / Displacement", (-900, -930))
    disp = get_or_create_node(mat, "HL2_Displacement", "ShaderNodeDisplacement", "Displacement", (-500, -930))

    detected = {
        "basecolor": None,
        "roughness": None,
        "metallic": None,
        "normal": None,
        "height": None,
    }

    for path in filepaths:
        texture_type = detect_pbr_type(os.path.basename(path))

        if texture_type and detected[texture_type] is None:
            detected[texture_type] = path

    loaded = []

    if detected["basecolor"] and assign_image_to_node(new_base, detected["basecolor"], "sRGB"):
        loaded.append("BaseColor")

    if detected["roughness"] and assign_image_to_node(new_rough, detected["roughness"], "Non-Color"):
        loaded.append("Roughness")

    if detected["metallic"] and assign_image_to_node(new_met, detected["metallic"], "Non-Color"):
        loaded.append("Metallic")

    if detected["normal"] and assign_image_to_node(new_norm, detected["normal"], "Non-Color"):
        loaded.append("Normal")

    if detected["height"] and assign_image_to_node(new_h, detected["height"], "Non-Color"):
        loaded.append("Height / Displacement")

    if new_base.image and "Base Color" in princ.inputs:
        connect_unique(mat, new_base.outputs["Color"], princ.inputs["Base Color"])

    if new_rough.image and "Roughness" in princ.inputs:
        connect_unique(mat, new_rough.outputs["Color"], princ.inputs["Roughness"])

    if new_met.image and "Metallic" in princ.inputs:
        connect_unique(mat, new_met.outputs["Color"], princ.inputs["Metallic"])

    if new_norm.image:
        connect_unique(mat, new_norm.outputs["Color"], norm_map.inputs["Color"])

        if "Normal" in princ.inputs:
            connect_unique(mat, norm_map.outputs["Normal"], princ.inputs["Normal"])

    if new_h.image:
        connect_unique(mat, new_h.outputs["Color"], disp.inputs["Height"])

        if "Midlevel" in disp.inputs:
            disp.inputs["Midlevel"].default_value = 0.5

        if "Displacement" in out.inputs:
            connect_unique(mat, disp.outputs["Displacement"], out.inputs["Displacement"])

    if not loaded:
        return False, "No PBR textures were detected. Check the filenames."

    missing = [key for key, value in detected.items() if value is None]
    message = "Loaded and connected: " + ", ".join(loaded)

    if missing:
        message += ". Missing: " + ", ".join(missing)

    return True, message


# -----------------------------------------------------------------------------
# Technical render for BaseColor / Roughness / Metallic
# -----------------------------------------------------------------------------

def get_principled_node(mat):
    if not mat or not mat.use_nodes:
        return None

    named = mat.node_tree.nodes.get("NEW_PBR_Principled")
    if named and named.bl_idname == "ShaderNodeBsdfPrincipled":
        return named

    return next((node for node in mat.node_tree.nodes if node.bl_idname == "ShaderNodeBsdfPrincipled"), None)


def output_node(mat):
    if not mat or not mat.use_nodes:
        return None

    return next((node for node in mat.node_tree.nodes if node.bl_idname == "ShaderNodeOutputMaterial"), None)


def named_node(mat, names):
    if not mat or not mat.use_nodes:
        return None

    for name in names:
        node = mat.node_tree.nodes.get(name)

        if node:
            return node

    return None


def make_constant(nodes, value, name):
    rgb = nodes.new("ShaderNodeRGB")
    rgb.name = name
    rgb.label = name

    if isinstance(value, (int, float)):
        rgb.outputs[0].default_value = (value, value, value, 1.0)
    else:
        try:
            rgb.outputs[0].default_value = value
        except Exception:
            rgb.outputs[0].default_value = (1.0, 1.0, 1.0, 1.0)

    return rgb


def create_export_material(original_mat, channel):
    if original_mat is None:
        return None

    mat = original_mat.copy()
    mat.name = f"HL2_TEMP_{channel}_{original_mat.name}"
    mat.use_nodes = True

    nodes = mat.node_tree.nodes
    links = mat.node_tree.links
    out = output_node(mat) or nodes.new("ShaderNodeOutputMaterial")

    em = nodes.new("ShaderNodeEmission")
    em.name = f"HL2_{channel}_Emission"
    em.label = f"{channel} Technical Output"
    em.location = (450, 0)

    if "Strength" in em.inputs:
        em.inputs["Strength"].default_value = 1.0

    source = None
    princ = get_principled_node(mat)

    if channel == "BaseColor":
        node = named_node(mat, ["NEW_BaseColor", "BaseColor", "Base Color"])

        if princ and "Base Color" in princ.inputs:
            if princ.inputs["Base Color"].links:
                source = princ.inputs["Base Color"].links[0].from_socket
            else:
                source = make_constant(nodes, princ.inputs["Base Color"].default_value, "BaseColor_Constant").outputs[0]
        elif node and node.bl_idname == "ShaderNodeTexImage" and node.image:
            source = node.outputs["Color"]

    elif channel == "Roughness":
        node = named_node(mat, ["NEW_Roughness", "Roughness"])

        if princ and "Roughness" in princ.inputs:
            if princ.inputs["Roughness"].links:
                source = princ.inputs["Roughness"].links[0].from_socket
            else:
                source = make_constant(nodes, princ.inputs["Roughness"].default_value, "Roughness_Constant").outputs[0]
        elif node and node.bl_idname == "ShaderNodeTexImage" and node.image:
            source = node.outputs["Color"]

    elif channel == "Metallic":
        node = named_node(mat, ["NEW_Metallic", "Metallic"])

        if princ and "Metallic" in princ.inputs:
            if princ.inputs["Metallic"].links:
                source = princ.inputs["Metallic"].links[0].from_socket
            else:
                source = make_constant(nodes, princ.inputs["Metallic"].default_value, "Metallic_Constant").outputs[0]
        elif node and node.bl_idname == "ShaderNodeTexImage" and node.image:
            source = node.outputs["Color"]

    if source is None:
        fallback = {
            "BaseColor": (1.0, 1.0, 1.0, 1.0),
            "Roughness": 0.6,
            "Metallic": 0.0,
        }.get(channel, 0.0)

        source = make_constant(nodes, fallback, f"{channel}_Fallback").outputs[0]

    links.new(source, em.inputs["Color"])

    for link in list(out.inputs["Surface"].links):
        links.remove(link)

    links.new(em.outputs["Emission"], out.inputs["Surface"])

    return mat


def collect_slots():
    records = []

    for obj in bpy.context.scene.objects:
        if obj.type == 'MESH' and obj.visible_get():
            for index, slot in enumerate(obj.material_slots):
                records.append((obj, index, slot.material))

    return records


def assign_temp_materials(channel):
    records = collect_slots()
    temps = []

    for obj, index, mat in records:
        if mat is None:
            continue

        temp = create_export_material(mat, channel)

        if temp:
            obj.material_slots[index].material = temp
            temps.append(temp)

    return records, temps


def restore_slots(records):
    for obj, index, mat in records:
        try:
            obj.material_slots[index].material = mat
        except Exception:
            pass


def remove_temps(temps):
    for mat in temps:
        try:
            bpy.data.materials.remove(mat)
        except Exception:
            pass


def render_technical_channel(scene, props, channel):
    path = out_path(props.output_folder, props.texture_name, channel, "png")
    records, temps = assign_temp_materials(channel)
    settings = save_render_settings(scene)

    try:
        setup_scene(scene, props, preview=False)

        scene.view_settings.view_transform = 'Standard'
        scene.view_settings.look = 'None'
        scene.view_settings.exposure = 0.0
        scene.view_settings.gamma = 1.0

        scene.render.filepath = path
        scene.render.image_settings.file_format = 'PNG'
        scene.render.image_settings.color_depth = '16'
        scene.render.image_settings.color_mode = 'RGB' if channel == "BaseColor" else 'BW'

        bpy.ops.render.render(write_still=True)

        ok, message = True, path

    except Exception as error:
        ok, message = False, str(error)

    restore_slots(records)
    remove_temps(temps)
    restore_render_settings(scene, settings)

    return ok, message



# -----------------------------------------------------------------------------
# Old Map
# -----------------------------------------------------------------------------

def create_old_map_material(props):
    mat = bpy.data.materials.get(OLD_MAP_MATERIAL_NAME)
    if mat is None:
        mat = bpy.data.materials.new(OLD_MAP_MATERIAL_NAME)
    mat.use_nodes = True
    nodes = mat.node_tree.nodes
    links = mat.node_tree.links
    nodes.clear()
    output = nodes.new("ShaderNodeOutputMaterial")
    output.name = "OLD_Map_Output"
    output.location = (650, 0)
    diffuse = nodes.new("ShaderNodeBsdfDiffuse")
    diffuse.name = "OLD_Map_Diffuse"
    diffuse.label = "OLD Map Diffuse"
    diffuse.location = (350, 0)
    links.new(diffuse.outputs["BSDF"], output.inputs["Surface"])
    img = load_image_safe(props.source_texture, "sRGB")
    if img:
        texcoord = nodes.new("ShaderNodeTexCoord")
        texcoord.location = (-800, 0)
        mapping = nodes.new("ShaderNodeMapping")
        mapping.location = (-580, 0)
        old_tex = nodes.new("ShaderNodeTexImage")
        old_tex.name = "OLD_Map_BaseColor"
        old_tex.label = "OLD Map BaseColor"
        old_tex.location = (-350, 0)
        old_tex.image = img
        try:
            old_tex.interpolation = 'Smart'
        except Exception:
            pass
        links.new(texcoord.outputs["UV"], mapping.inputs["Vector"])
        links.new(mapping.outputs["Vector"], old_tex.inputs["Vector"])
        links.new(old_tex.outputs["Color"], diffuse.inputs["Color"])
    return mat


def create_or_update_old_map_plane(props, create_if_missing=True):
    collection = ensure_collection(ADDON_COLLECTION_NAME)
    original_plane = bpy.data.objects.get(PLANE_NAME)
    plane_size = props.plane_size
    mode = props.setup_mode
    rotation = plane_rotation_for_mode(mode)
    if original_plane:
        base_location = original_plane.location.copy()
        old_location = (base_location.x - plane_size - 0.20, base_location.y, base_location.z)
    else:
        old_location = (-plane_size - 0.20, 0.0, 0.0)
    old_plane = bpy.data.objects.get(OLD_MAP_PLANE_NAME)
    if old_plane is None:
        if not create_if_missing:
            return None
        bpy.ops.mesh.primitive_plane_add(size=plane_size, location=old_location, rotation=rotation)
        old_plane = bpy.context.object
        old_plane.name = OLD_MAP_PLANE_NAME
        move_to_collection(old_plane, collection)
    else:
        old_plane.location = old_location
        old_plane.rotation_euler = rotation
        old_plane.scale = (1.0, 1.0, 1.0)
        move_to_collection(old_plane, collection)
    bpy.ops.object.select_all(action='DESELECT')
    bpy.context.view_layer.objects.active = old_plane
    old_plane.select_set(True)
    try:
        bpy.ops.object.transform_apply(location=False, rotation=False, scale=True)
    except Exception:
        pass
    old_plane.rotation_euler = rotation
    mat = create_old_map_material(props)
    assign_material(old_plane, mat)
    return old_plane


# -----------------------------------------------------------------------------
# Test Maps
# -----------------------------------------------------------------------------

def create_test_maps_material(props):
    texture_name = props.texture_name
    output_folder = bpy.path.abspath(props.output_folder)

    basecolor_path = find_exported_texture(output_folder, texture_name, "BaseColor", ["png", "jpg", "jpeg", "tif", "tiff"])
    roughness_path = find_exported_texture(output_folder, texture_name, "Roughness", ["png", "jpg", "jpeg", "tif", "tiff"])
    metallic_path = find_exported_texture(output_folder, texture_name, "Metallic", ["png", "jpg", "jpeg", "tif", "tiff"])
    normal_path = find_exported_texture(output_folder, texture_name, "Normal", ["png", "jpg", "jpeg", "tif", "tiff"])
    displacement_path = find_exported_texture(output_folder, texture_name, "Displacement", ["exr", "png", "tif", "tiff"])

    mat_name = f"{TEST_MATERIAL_PREFIX}{texture_name}"
    old_mat = bpy.data.materials.get(mat_name)

    if old_mat:
        try:
            bpy.data.materials.remove(old_mat)
        except Exception:
            pass

    mat = bpy.data.materials.new(mat_name)
    mat.use_nodes = True
    safe_set_material_displacement(mat)

    nodes = mat.node_tree.nodes
    links = mat.node_tree.links
    nodes.clear()

    output = nodes.new("ShaderNodeOutputMaterial")
    output.name = "TEST_Material_Output"
    output.location = (900, 0)

    principled = nodes.new("ShaderNodeBsdfPrincipled")
    principled.name = "TEST_Principled"
    principled.label = "TEST Exported Maps Principled"
    principled.location = (500, 100)

    links.new(principled.outputs["BSDF"], output.inputs["Surface"])

    texcoord = nodes.new("ShaderNodeTexCoord")
    texcoord.location = (-900, 100)

    mapping = nodes.new("ShaderNodeMapping")
    mapping.location = (-700, 100)

    links.new(texcoord.outputs["UV"], mapping.inputs["Vector"])

    if basecolor_path:
        base_img = load_image_safe(basecolor_path, "sRGB")
        base_tex = nodes.new("ShaderNodeTexImage")
        base_tex.name = "TEST_BaseColor"
        base_tex.label = "TEST BaseColor"
        base_tex.location = (-450, 300)
        base_tex.image = base_img
        links.new(mapping.outputs["Vector"], base_tex.inputs["Vector"])
        links.new(base_tex.outputs["Color"], principled.inputs["Base Color"])

    if roughness_path:
        rough_img = load_image_safe(roughness_path, "Non-Color")
        rough_tex = nodes.new("ShaderNodeTexImage")
        rough_tex.name = "TEST_Roughness"
        rough_tex.label = "TEST Roughness"
        rough_tex.location = (-450, 80)
        rough_tex.image = rough_img
        links.new(mapping.outputs["Vector"], rough_tex.inputs["Vector"])

        if "Roughness" in principled.inputs:
            links.new(rough_tex.outputs["Color"], principled.inputs["Roughness"])

    if metallic_path:
        met_img = load_image_safe(metallic_path, "Non-Color")
        met_tex = nodes.new("ShaderNodeTexImage")
        met_tex.name = "TEST_Metallic"
        met_tex.label = "TEST Metallic"
        met_tex.location = (-450, -120)
        met_tex.image = met_img
        links.new(mapping.outputs["Vector"], met_tex.inputs["Vector"])

        if "Metallic" in principled.inputs:
            links.new(met_tex.outputs["Color"], principled.inputs["Metallic"])

    if normal_path:
        norm_img = load_image_safe(normal_path, "Non-Color")
        norm_tex = nodes.new("ShaderNodeTexImage")
        norm_tex.name = "TEST_Normal"
        norm_tex.label = "TEST Normal"
        norm_tex.location = (-450, -340)
        norm_tex.image = norm_img

        normal_map = nodes.new("ShaderNodeNormalMap")
        normal_map.name = "TEST_NormalMap"
        normal_map.label = "TEST Normal Map"
        normal_map.location = (-160, -340)

        links.new(mapping.outputs["Vector"], norm_tex.inputs["Vector"])
        links.new(norm_tex.outputs["Color"], normal_map.inputs["Color"])

        if "Normal" in principled.inputs:
            links.new(normal_map.outputs["Normal"], principled.inputs["Normal"])

    if displacement_path:
        disp_img = load_image_safe(displacement_path, "Non-Color")
        disp_tex = nodes.new("ShaderNodeTexImage")
        disp_tex.name = "TEST_Displacement"
        disp_tex.label = "TEST Displacement"
        disp_tex.location = (-450, -620)
        disp_tex.image = disp_img

        displacement = nodes.new("ShaderNodeDisplacement")
        displacement.name = "TEST_Displacement_Node"
        displacement.label = "TEST Displacement"
        displacement.location = (-160, -620)

        if "Scale" in displacement.inputs:
            displacement.inputs["Scale"].default_value = 1.0

        if "Midlevel" in displacement.inputs:
            displacement.inputs["Midlevel"].default_value = 0.5

        links.new(mapping.outputs["Vector"], disp_tex.inputs["Vector"])
        links.new(disp_tex.outputs["Color"], displacement.inputs["Height"])
        links.new(displacement.outputs["Displacement"], output.inputs["Displacement"])

    return mat


def create_or_update_test_maps_plane(props):
    collection = ensure_collection(ADDON_COLLECTION_NAME)
    original_plane = bpy.data.objects.get(PLANE_NAME)
    plane_size = props.plane_size
    mode = props.setup_mode
    rotation = plane_rotation_for_mode(mode)

    if original_plane:
        base_location = original_plane.location.copy()
        test_location = (
            base_location.x + plane_size + 0.20,
            base_location.y,
            base_location.z,
        )
    else:
        test_location = (plane_size + 0.20, 0.0, 0.0)

    test_plane = bpy.data.objects.get(TEST_PLANE_NAME)

    if test_plane is None:
        bpy.ops.mesh.primitive_plane_add(size=plane_size, location=test_location, rotation=rotation)
        test_plane = bpy.context.object
        test_plane.name = TEST_PLANE_NAME
        move_to_collection(test_plane, collection)
    else:
        test_plane.location = test_location
        test_plane.rotation_euler = rotation
        test_plane.scale = (1.0, 1.0, 1.0)
        move_to_collection(test_plane, collection)

    bpy.ops.object.select_all(action='DESELECT')
    bpy.context.view_layer.objects.active = test_plane
    test_plane.select_set(True)

    try:
        bpy.ops.object.transform_apply(location=False, rotation=False, scale=True)
    except Exception:
        pass

    test_plane.rotation_euler = rotation

    subsurf = test_plane.modifiers.get("HL2_Test_Subdivision")

    if subsurf is None:
        subsurf = test_plane.modifiers.new("HL2_Test_Subdivision", 'SUBSURF')

    try:
        subsurf.subdivision_type = 'SIMPLE'
        subsurf.levels = 3
        subsurf.render_levels = 3
        subsurf.show_on_cage = True
    except Exception:
        pass

    safe_set_adaptive_subdivision(test_plane, subsurf)

    mat = create_test_maps_material(props)
    assign_material(test_plane, mat)
    safe_set_material_displacement(mat)

    return test_plane


# -----------------------------------------------------------------------------
# Compositor pass exports
# -----------------------------------------------------------------------------

def get_compositor_tree(scene):
    scene.use_nodes = True

    if hasattr(scene, "compositing_node_group"):
        try:
            if scene.compositing_node_group is None:
                scene.compositing_node_group = bpy.data.node_groups.new("HL2_Compositor", "CompositorNodeTree")
            return scene.compositing_node_group
        except Exception:
            pass

    try:
        return scene.node_tree
    except Exception:
        return None


def socket_names(sockets):
    return ", ".join([socket.name for socket in sockets])


def socket_by_names(sockets, names):
    for name in names:
        if name in sockets:
            return sockets[name]

    simple = {
        socket.name.lower().replace(" ", "").replace("_", ""): socket
        for socket in sockets
    }

    for name in names:
        key = name.lower().replace(" ", "").replace("_", "")

        if key in simple:
            return simple[key]

    return None


def create_viewer(nodes, name, label, location):
    viewer = nodes.new(type="CompositorNodeViewer")
    viewer.name = name
    viewer.label = label
    viewer.location = location

    for node in nodes:
        node.select = False

    viewer.select = True
    nodes.active = viewer

    return viewer


def enable_pass(view_layer, pass_name):
    props = []

    if pass_name == "Normal":
        props = ["use_pass_normal"]

    elif pass_name == "Depth":
        props = ["use_pass_z"]

    for prop_name in props:
        try:
            if hasattr(view_layer, prop_name):
                setattr(view_layer, prop_name, True)
        except Exception:
            pass


def setup_normal_pass_viewer(scene):
    scene.use_nodes = True
    scene.render.use_compositing = True

    enable_pass(bpy.context.view_layer, "Normal")

    tree = get_compositor_tree(scene)

    if tree is None:
        return False, "Could not access compositor node tree"

    nodes = tree.nodes
    links = tree.links
    nodes.clear()
    clear_viewer_images()

    render_layers = nodes.new(type="CompositorNodeRLayers")
    render_layers.name = "HL2_RenderLayers_Normal"
    render_layers.label = "Render Layers / Normal Source"
    render_layers.location = (-500, 0)

    socket = socket_by_names(render_layers.outputs, ["Normal", "Normals"])

    if socket is None:
        return False, f"Render Layers node has no Normal output. Available outputs: {socket_names(render_layers.outputs)}"

    viewer = create_viewer(nodes, "HL2_NormalViewer", "HL2 Normal Viewer", (0, 0))
    links.new(socket, viewer.inputs["Image"])

    return True, "Normal viewer ready"


def save_front_normal_from_viewer(scene, img, path):
    if img is None:
        return False, "Viewer image not found"

    width, height = img.size

    if width <= 0 or height <= 0:
        return False, "Invalid normal image size"

    total = width * height * 4
    source = array.array('f', [0.0]) * total
    img.pixels.foreach_get(source)

    converted = array.array('f', [0.0]) * total

    for i in range(0, total, 4):
        nx = source[i]
        ny = source[i + 1]
        nz = source[i + 2]
        alpha = source[i + 3]

        converted[i] = clamp01(nx * 0.5 + 0.5)
        converted[i + 1] = clamp01(nz * 0.5 + 0.5)
        converted[i + 2] = clamp01((-ny) * 0.5 + 0.5)
        converted[i + 3] = alpha

    normal_img = bpy.data.images.new(
        name=f"HL2_Converted_Front_Normal_{width}x{height}",
        width=width,
        height=height,
        alpha=True,
        float_buffer=False,
    )

    normal_img.pixels.foreach_set(converted)
    normal_img.update()

    settings = save_render_settings(scene)

    try:
        scene.render.image_settings.file_format = 'PNG'
        scene.render.image_settings.color_mode = 'RGB'
        scene.render.image_settings.color_depth = '16'

        normal_img.save_render(path, scene=scene)
        restore_render_settings(scene, settings)

        if os.path.exists(path):
            show_image_in_image_editor(normal_img)
            return True, path

        return False, f"Normal PNG was not created: {path}"

    except Exception as error:
        restore_render_settings(scene, settings)
        return False, f"Could not save converted front normal: {error}"


def export_normal_pass(scene, props):
    path = out_path(props.output_folder, props.texture_name, "Normal", "png")
    settings = save_render_settings(scene)
    disp_records = displacement_scale_records()

    try:
        set_displacement_scales(disp_records, 0.0)
        setup_scene(scene, props, preview=False)
        scene.view_settings.view_transform = 'Standard'
        scene.view_settings.look = 'None'
        scene.view_settings.exposure = 0.0
        scene.view_settings.gamma = 1.0
        ok, message = setup_normal_pass_viewer(scene)
        if not ok:
            restore_displacement_scales(disp_records)
            restore_render_settings(scene, settings)
            return False, message
        bpy.ops.render.render(write_still=False)
        img = rename_latest_viewer("Normal")
        if props.setup_mode == MODE_FRONT:
            ok, message = save_front_normal_from_viewer(scene, img, path)
        else:
            show_image_in_image_editor(img)
            ok, message = save_image_to_path(img, path)
        restore_displacement_scales(disp_records)
        restore_render_settings(scene, settings)
        return ok, message
    except Exception as error:
        restore_displacement_scales(disp_records)
        restore_render_settings(scene, settings)
        return False, str(error)


# -----------------------------------------------------------------------------
# Depth / displacement compositor
# -----------------------------------------------------------------------------

def setup_blur_node(node, amount):
    try:
        node.filter_type = 'GAUSS'
    except Exception:
        pass

    try:
        node.size_x = int(amount)
        node.size_y = int(amount)
        node.use_relative = False
    except Exception:
        pass


def get_plane_base_depth(props):
    return props.camera_z_distance


def get_symmetric_depth_extent(props):
    return max(float(props.depth_front_range), float(props.depth_back_range), 0.0001)


def setup_depth_viewer(scene, props):
    scene.use_nodes = True
    scene.render.use_compositing = True

    try:
        bpy.context.view_layer.use_pass_z = True
    except Exception as error:
        return False, f"Could not enable Depth/Z pass: {error}"

    tree = get_compositor_tree(scene)

    if tree is None:
        return False, "Could not access compositor node tree"

    nodes = tree.nodes
    links = tree.links
    nodes.clear()
    clear_viewer_images()

    render_layers = nodes.new(type="CompositorNodeRLayers")
    render_layers.name = "HL2_RenderLayers_Depth"
    render_layers.label = "Render Layers / Depth Source"
    render_layers.location = (-760, 0)

    depth_socket = None

    if "Depth" in render_layers.outputs:
        depth_socket = render_layers.outputs["Depth"]
    elif "Z" in render_layers.outputs:
        depth_socket = render_layers.outputs["Z"]

    if depth_socket is None:
        return False, f"Render Layers node has no Depth or Z output. Available outputs: {socket_names(render_layers.outputs)}"

    base_depth = get_plane_base_depth(props)
    extent = get_symmetric_depth_extent(props)

    from_min = base_depth - extent
    from_max = base_depth + extent

    map_range = nodes.new(type="ShaderNodeMapRange")
    map_range.name = "HL2_FixedDepthRange"
    map_range.label = "Fixed Depth Range. Base Plane = 0.5"
    map_range.location = (-430, 0)

    try:
        map_range.data_type = 'FLOAT'
        map_range.interpolation_type = 'LINEAR'
        map_range.clamp = True
    except Exception:
        pass

    map_range.inputs[1].default_value = from_min
    map_range.inputs[2].default_value = from_max
    map_range.inputs[3].default_value = 1.0
    map_range.inputs[4].default_value = 0.0

    blur = nodes.new(type="CompositorNodeBlur")
    blur.name = "HL2_DepthBlur"
    blur.label = "Depth Blur"
    blur.location = (-160, 0)

    setup_blur_node(blur, props.depth_blur)

    viewer = create_viewer(nodes, "HL2_DepthViewer", "HL2 Depth Viewer", (120, 0))

    links.new(depth_socket, map_range.inputs[0])
    links.new(map_range.outputs[0], blur.inputs["Image"])
    links.new(blur.outputs["Image"], viewer.inputs["Image"])

    return True, "Depth viewer ready. Base plane maps to 0.5."


def render_depth_viewer(scene, props):
    setup_scene(scene, props, preview=False)
    apply_displacement_only_to_all_materials()
    set_cycles_dicing(scene)

    ok, message = setup_depth_viewer(scene, props)

    if not ok:
        return False, message, None

    try:
        scene.view_settings.view_transform = 'Standard'
        scene.view_settings.look = 'None'
        scene.view_settings.exposure = 0.0
        scene.view_settings.gamma = 1.0

        bpy.ops.render.render(write_still=False)

    except Exception as error:
        return False, f"Depth render failed: {error}", None

    img = rename_latest_viewer("Depth")
    show_image_in_image_editor(img)

    return True, message, img


def export_displacement(scene, props):
    output_folder = bpy.path.abspath(props.output_folder)
    ensure_output_folder(output_folder)

    final_path = os.path.join(output_folder, f"{props.texture_name}_Displacement.exr")
    settings = save_render_settings(scene)

    try:
        ok, message, img = render_depth_viewer(scene, props)

        if not ok:
            restore_render_settings(scene, settings)
            return False, message

        ok, message = save_viewer_as_exr(scene, img, final_path)
        restore_render_settings(scene, settings)

        return ok, message

    except Exception as error:
        restore_render_settings(scene, settings)
        return False, str(error)


# -----------------------------------------------------------------------------
# UV Tools - Rectify
# -----------------------------------------------------------------------------

def hl2_uv_is_uv_editor(context):
    area = context.area

    if not area:
        return False

    if area.type != 'IMAGE_EDITOR':
        return False

    try:
        return area.ui_type == 'UV'
    except Exception:
        return True


def hl2_get_uv_selected(loop, uv_layer):
    luv = loop[uv_layer]

    if hasattr(luv, "select_vert"):
        try:
            return bool(luv.select_vert)
        except Exception:
            pass

    if hasattr(luv, "select"):
        try:
            return bool(luv.select)
        except Exception:
            pass

    try:
        return bool(loop.vert.select)
    except Exception:
        pass

    return False


def hl2_set_uv_selected(loop, uv_layer, value):
    luv = loop[uv_layer]

    if hasattr(luv, "select_vert"):
        try:
            luv.select_vert = value
        except Exception:
            pass

    if hasattr(luv, "select_edge"):
        try:
            luv.select_edge = value
        except Exception:
            pass

    if hasattr(luv, "select"):
        try:
            luv.select = value
        except Exception:
            pass


def hl2_uv_selection_store(bm, uv_layer):
    data = []

    for face in bm.faces:
        face_data = {
            "face": face,
            "face_select": face.select,
            "loops": [],
        }

        for loop in face.loops:
            luv = loop[uv_layer]
            face_data["loops"].append(
                (
                    loop,
                    getattr(luv, "select_vert", None),
                    getattr(luv, "select_edge", None),
                    getattr(luv, "select", None),
                )
            )

        data.append(face_data)

    return data


def hl2_uv_selection_restore(bm, uv_layer, data):
    for face_data in data:
        face = face_data["face"]

        try:
            face.select = face_data["face_select"]
        except Exception:
            pass

        for loop, select_vert, select_edge, select_old in face_data["loops"]:
            luv = loop[uv_layer]

            if select_vert is not None and hasattr(luv, "select_vert"):
                try:
                    luv.select_vert = select_vert
                except Exception:
                    pass

            if select_edge is not None and hasattr(luv, "select_edge"):
                try:
                    luv.select_edge = select_edge
                except Exception:
                    pass

            if select_old is not None and hasattr(luv, "select"):
                try:
                    luv.select = select_old
                except Exception:
                    pass


def hl2_get_selected_faces_loops(bm, uv_layer):
    faces_loops = {}

    for face in bm.faces:
        selected_loops = []

        for loop in face.loops:
            if hl2_get_uv_selected(loop, uv_layer):
                selected_loops.append(loop)

        if selected_loops:
            faces_loops[face] = selected_loops

        elif face.select:
            faces_loops[face] = list(face.loops)

    return faces_loops


def hl2_get_selected_islands(bm, selected_faces):
    selected_faces = set(selected_faces)
    islands = []

    while selected_faces:
        start = selected_faces.pop()
        island = {start}
        stack = [start]

        while stack:
            face = stack.pop()

            for edge in face.edges:
                for linked_face in edge.link_faces:
                    if linked_face in selected_faces:
                        selected_faces.remove(linked_face)
                        island.add(linked_face)
                        stack.append(linked_face)

        islands.append(island)

    return islands


def hl2_uv_image_ratio():
    ratio_x, ratio_y = 256, 256

    try:
        for area in bpy.context.screen.areas:
            if area.type == 'IMAGE_EDITOR':
                img = area.spaces[0].image

                if img and img.size[0] != 0:
                    ratio_x, ratio_y = img.size[0], img.size[1]

                break
    except Exception:
        pass

    return ratio_x, ratio_y


def hl2_are_uvs_quasi_equal(v1, v2, allowed_error=0.00001):
    return (
        abs(v1.uv.x - v2.uv.x) < allowed_error
        and abs(v1.uv.y - v2.uv.y) < allowed_error
    )


def hl2_hypot_uv(v1, v2):
    return hypot(v1.x - v2.x, v1.y - v2.y)


def hl2_lists_of_verts(bm, uv_layer, selected_faces_mix, faces_loops):
    all_edge_verts = []
    filtered_verts = []
    selected_faces = []
    discarded_faces = set()
    verts_dict = defaultdict(list)

    for face in selected_faces_mix:
        if face not in faces_loops:
            continue

        is_face_selected = True
        face_edge_verts = [loop[uv_layer] for loop in faces_loops[face]]

        if len(faces_loops[face]) < len(face.loops):
            is_face_selected = False

        all_edge_verts.extend(face_edge_verts)

        if is_face_selected:
            if len(face.verts) != 4:
                filtered_verts.extend(face_edge_verts)
                discarded_faces.add(face)
            else:
                selected_faces.append(face)

                for luv in face_edge_verts:
                    x = round(luv.uv.x, UV_RECTIFY_PRECISION)
                    y = round(luv.uv.y, UV_RECTIFY_PRECISION)
                    verts_dict[(x, y)].append(luv)
        else:
            filtered_verts.extend(face_edge_verts)

    if len(filtered_verts) == 0:
        filtered_verts.extend(all_edge_verts)

    return filtered_verts, selected_faces, verts_dict, discarded_faces


def hl2_make_uv_face_equal_rectangle(verts_dict, left_up, right_up, right_down, left_down, start_v):
    if start_v is None:
        start_v = left_up.uv
    elif hl2_are_uvs_quasi_equal(start_v, right_up):
        start_v = right_up.uv
    elif hl2_are_uvs_quasi_equal(start_v, right_down):
        start_v = right_down.uv
    elif hl2_are_uvs_quasi_equal(start_v, left_down):
        start_v = left_down.uv
    else:
        start_v = left_up.uv

    left_up_uv = left_up.uv
    right_up_uv = right_up.uv
    right_down_uv = right_down.uv
    left_down_uv = left_down.uv

    if start_v == left_up_uv:
        final_scale_x = hl2_hypot_uv(left_up_uv, right_up_uv)
        final_scale_y = hl2_hypot_uv(left_up_uv, left_down_uv)
        current_row_x = left_up_uv.x
        current_row_y = left_up_uv.y

    elif start_v == right_up_uv:
        final_scale_x = hl2_hypot_uv(right_up_uv, left_up_uv)
        final_scale_y = hl2_hypot_uv(right_up_uv, right_down_uv)
        current_row_x = right_up_uv.x - final_scale_x
        current_row_y = right_up_uv.y

    elif start_v == right_down_uv:
        final_scale_x = hl2_hypot_uv(right_down_uv, left_down_uv)
        final_scale_y = hl2_hypot_uv(right_down_uv, right_up_uv)
        current_row_x = right_down_uv.x - final_scale_x
        current_row_y = right_down_uv.y + final_scale_y

    else:
        final_scale_x = hl2_hypot_uv(left_down_uv, right_down_uv)
        final_scale_y = hl2_hypot_uv(left_down_uv, left_up_uv)
        current_row_x = left_down_uv.x
        current_row_y = left_down_uv.y + final_scale_y

    x = round(left_up_uv.x, UV_RECTIFY_PRECISION)
    y = round(left_up_uv.y, UV_RECTIFY_PRECISION)

    for v in verts_dict[(x, y)]:
        v.uv.x = current_row_x
        v.uv.y = current_row_y

    x = round(right_up_uv.x, UV_RECTIFY_PRECISION)
    y = round(right_up_uv.y, UV_RECTIFY_PRECISION)

    for v in verts_dict[(x, y)]:
        v.uv.x = current_row_x + final_scale_x
        v.uv.y = current_row_y

    x = round(right_down_uv.x, UV_RECTIFY_PRECISION)
    y = round(right_down_uv.y, UV_RECTIFY_PRECISION)

    for v in verts_dict[(x, y)]:
        v.uv.x = current_row_x + final_scale_x
        v.uv.y = current_row_y - final_scale_y

    x = round(left_down_uv.x, UV_RECTIFY_PRECISION)
    y = round(left_down_uv.y, UV_RECTIFY_PRECISION)

    for v in verts_dict[(x, y)]:
        v.uv.x = current_row_x
        v.uv.y = current_row_y - final_scale_y


def hl2_shape_face(uv_layer, target_face, verts_dict):
    corners = []

    for loop in target_face.loops:
        corners.append(loop[uv_layer])

    if len(corners) != 4:
        return

    first_highest = corners[0]

    for corner in corners:
        if corner.uv.y > first_highest.uv.y:
            first_highest = corner

    corners.remove(first_highest)

    second_highest = corners[0]

    for corner in corners:
        if corner.uv.y > second_highest.uv.y:
            second_highest = corner

    if first_highest.uv.x < second_highest.uv.x:
        left_up = first_highest
        right_up = second_highest
    else:
        left_up = second_highest
        right_up = first_highest

    corners.remove(second_highest)

    first_lowest = corners[0]
    second_lowest = corners[1]

    if first_lowest.uv.x < second_lowest.uv.x:
        left_down = first_lowest
        right_down = second_lowest
    else:
        left_down = second_lowest
        right_down = first_lowest

    verts = [left_up, left_down, right_down, right_up]
    ratio_x, ratio_y = hl2_uv_image_ratio()
    min_distance = float('inf')
    min_v = verts[0]

    try:
        for area in bpy.context.screen.areas:
            if area.type == 'IMAGE_EDITOR':
                loc = area.spaces[0].cursor_location

                for v in verts:
                    hyp = hypot(loc.x / ratio_x - v.uv.x, loc.y / ratio_y - v.uv.y)

                    if hyp < min_distance:
                        min_distance = hyp
                        min_v = v
    except Exception:
        min_v = left_up

    hl2_make_uv_face_equal_rectangle(
        verts_dict,
        left_up,
        right_up,
        right_down,
        left_down,
        min_v,
    )


def hl2_follow_active_uv(me, active_face, faces):
    bm = bmesh.from_edit_mesh(me)
    uv_act = bm.loops.layers.uv.active

    def walk_face_init(faces_arg, active_face_arg):
        for face in bm.faces:
            face.tag = True

        for face in faces_arg:
            face.tag = False

        active_face_arg.tag = True

    def walk_face(face):
        face.tag = True
        faces_a = [face]
        faces_b = []

        while faces_a:
            for face_a in faces_a:
                for loop in face_a.loops:
                    loop_edge = loop.edge

                    if loop_edge.is_manifold and not loop_edge.seam:
                        loop_other = loop.link_loop_radial_next
                        face_other = loop_other.face

                        if not face_other.tag:
                            yield (face_a, loop, face_other)
                            face_other.tag = True
                            faces_b.append(face_other)

            faces_a, faces_b = faces_b, faces_a
            faces_b.clear()

    def walk_edgeloop(loop):
        first_edge = loop.edge

        while True:
            edge = loop.edge
            yield edge

            if edge.is_manifold:
                loop = loop.link_loop_radial_next

                if len(loop.face.verts) == 4:
                    loop = loop.link_loop_next.link_loop_next

                    if loop.edge is first_edge:
                        break
                else:
                    break
            else:
                break

    def extrapolate_uv(factor, loop_a_outer, loop_a_inner, loop_b_outer, loop_b_inner):
        loop_b_inner[:] = loop_a_inner
        loop_b_outer[:] = loop_a_inner + ((loop_a_inner - loop_a_outer) * factor)

    def apply_uv(face_prev, loop_prev, face_next):
        loops_a = [None, None, None, None]
        loops_b = [None, None, None, None]

        loops_a[0] = loop_prev
        loops_a[1] = loops_a[0].link_loop_next
        loops_a[2] = loops_a[1].link_loop_next
        loops_a[3] = loops_a[2].link_loop_next

        loop_next = loop_prev.link_loop_radial_next

        if loop_next.vert != loop_prev.vert:
            loops_b[1] = loop_next
            loops_b[0] = loops_b[1].link_loop_next
            loops_b[3] = loops_b[0].link_loop_next
            loops_b[2] = loops_b[3].link_loop_next
        else:
            loops_b[0] = loop_next
            loops_b[1] = loops_b[0].link_loop_next
            loops_b[2] = loops_b[1].link_loop_next
            loops_b[3] = loops_b[2].link_loop_next

        loops_a_uv = [loop[uv_act].uv for loop in loops_a]
        loops_b_uv = [loop[uv_act].uv for loop in loops_b]

        try:
            factor = edge_lengths[loops_b[2].edge.index][0] / edge_lengths[loops_a[1].edge.index][0]
        except Exception:
            factor = 1.0

        extrapolate_uv(factor, loops_a_uv[3], loops_a_uv[0], loops_b_uv[3], loops_b_uv[0])
        extrapolate_uv(factor, loops_a_uv[2], loops_a_uv[1], loops_b_uv[2], loops_b_uv[1])

    bm.edges.index_update()
    edge_lengths = [None] * len(bm.edges)

    for face in faces:
        if len(face.loops) != 4:
            continue

        loops_quad = face.loops[:]
        loop_pair_a = (loops_quad[0], loops_quad[2])
        loop_pair_b = (loops_quad[1], loops_quad[3])

        for loop_pair in (loop_pair_a, loop_pair_b):
            if edge_lengths[loop_pair[0].edge.index] is None:
                edge_length_store = [-1.0]
                edge_length_accum = 0.0
                edge_length_total = 0

                for loop in loop_pair:
                    if edge_lengths[loop.edge.index] is None:
                        for edge in walk_edgeloop(loop):
                            if edge_lengths[edge.index] is None:
                                edge_lengths[edge.index] = edge_length_store
                                edge_length_accum += edge.calc_length()
                                edge_length_total += 1

                if edge_length_total > 0:
                    edge_length_store[0] = edge_length_accum / edge_length_total
                else:
                    edge_length_store[0] = 1.0

    walk_face_init(faces, active_face)

    for face_triple in walk_face(active_face):
        apply_uv(*face_triple)


def hl2_rectify_island(me, bm, uv_layer, selected_faces_mix, faces_loops):
    filtered_verts, selected_faces, verts_dict, discarded_faces = hl2_lists_of_verts(
        bm,
        uv_layer,
        selected_faces_mix,
        faces_loops,
    )

    if len(filtered_verts) < 2:
        return

    if not selected_faces:
        for luv in filtered_verts:
            x = round(luv.uv.x, UV_RECTIFY_PRECISION)
            y = round(luv.uv.y, UV_RECTIFY_PRECISION)

            if luv not in verts_dict[(x, y)]:
                verts_dict[(x, y)].append(luv)

        are_lined_x = True
        are_lined_y = True
        allowed_error = 0.00001
        val_x = filtered_verts[0].uv.x
        val_y = filtered_verts[0].uv.y

        for vert in filtered_verts:
            if abs(val_x - vert.uv.x) > allowed_error:
                are_lined_x = False

            if abs(val_y - vert.uv.y) > allowed_error:
                are_lined_y = False

        if are_lined_x or are_lined_y:
            return

        verts = filtered_verts
        verts.sort(key=lambda x: x.uv[0])

        first = verts[0]
        last = verts[-1]

        horizontal = True

        if (last.uv.x - first.uv.x) > 0.0009:
            slope = (last.uv.y - first.uv.y) / (last.uv.x - first.uv.x)

            if slope > 1 or slope < -1:
                horizontal = False
        else:
            horizontal = False

        if horizontal:
            for vert in verts:
                x = round(vert.uv.x, UV_RECTIFY_PRECISION)
                y = round(vert.uv.y, UV_RECTIFY_PRECISION)

                for luv in verts_dict[(x, y)]:
                    luv.uv.y = first.uv.y
        else:
            verts.sort(key=lambda x: x.uv[1])
            verts.reverse()

            first = verts[0]

            for vert in verts:
                x = round(vert.uv.x, UV_RECTIFY_PRECISION)
                y = round(vert.uv.y, UV_RECTIFY_PRECISION)

                for luv in verts_dict[(x, y)]:
                    luv.uv.x = first.uv.x

        return

    active_face = bm.faces.active

    if (
        active_face is None
        or active_face.select is False
        or len(active_face.verts) != 4
    ):
        active_face = selected_faces[0]

    hl2_shape_face(uv_layer, active_face, verts_dict)
    hl2_follow_active_uv(me, active_face, selected_faces)
    bmesh.update_edit_mesh(me, loop_triangles=False)


def hl2_rectify_uv_selection(context):
    obj = context.active_object

    if obj is None or obj.type != 'MESH' or obj.mode != 'EDIT':
        return False, "Select a mesh object in Edit Mode"

    me = obj.data
    bm = bmesh.from_edit_mesh(me)
    uv_layer = bm.loops.layers.uv.verify()

    faces_loops = hl2_get_selected_faces_loops(bm, uv_layer)

    if not faces_loops:
        return False, "No UV faces or vertices selected"

    selection_data = hl2_uv_selection_store(bm, uv_layer)

    try:
        islands = hl2_get_selected_islands(bm, set(faces_loops.keys()))

        for island in islands:
            hl2_rectify_island(me, bm, uv_layer, island, faces_loops)

        bmesh.update_edit_mesh(me, loop_triangles=False)
        hl2_uv_selection_restore(bm, uv_layer, selection_data)
        bmesh.update_edit_mesh(me, loop_triangles=False)

        return True, "UV Rectify finished"

    except Exception as error:
        hl2_uv_selection_restore(bm, uv_layer, selection_data)
        bmesh.update_edit_mesh(me, loop_triangles=False)
        return False, str(error)


# -----------------------------------------------------------------------------
# Properties
# -----------------------------------------------------------------------------

def update_camera_bg_opacity(self, context):
    try:
        set_camera_background_opacity(context.scene, self.camera_bg_opacity)
    except Exception:
        pass


def update_source_as_camera_bg(self, context):
    try:
        if self.use_source_as_camera_background:
            set_camera_background_from_source(context.scene, self)
        else:
            cam = context.scene.camera or bpy.data.objects.get(CAMERA_NAME)
            remove_camera_backgrounds(cam)
    except Exception:
        pass


def update_render_resolution(self, context):
    try:
        apply_resolution(context.scene, self.resolution)
    except Exception:
        pass


class HL2RemasterProperties(bpy.types.PropertyGroup):
    texture_name: bpy.props.StringProperty(
        name="Texture Name",
        default="brickwall014a",
    )

    source_texture: bpy.props.StringProperty(
        name="Source Base Color",
        subtype='FILE_PATH',
    )

    working_blend_folder: bpy.props.StringProperty(
        name="Working Blend Folder",
        subtype='DIR_PATH',
    )

    save_blend_before_setup: bpy.props.BoolProperty(
        name="Save Blend Before Setup",
        description="Before creating a setup, save the blend file using the Texture Name inside the Working Blend Folder",
        default=True,
    )

    preserve_node_tree_on_setup: bpy.props.BoolProperty(
        name="Preserve Node Tree on New Setup",
        description="Keep the current material node tree when creating a setup for a new texture. The material is renamed and the OLD Base image is updated.",
        default=False,
    )


    use_source_as_camera_background: bpy.props.BoolProperty(
        name="Source as Camera BG",
        description="Use the Source Base Color as a front camera background reference",
        default=True,
        update=update_source_as_camera_bg,
    )

    camera_bg_opacity: bpy.props.FloatProperty(
        name="Camera BG Opacity",
        default=0.35,
        min=0.0,
        max=1.0,
        precision=2,
        update=update_camera_bg_opacity,
    )

    output_folder: bpy.props.StringProperty(
        name="Output Folder",
        subtype='DIR_PATH',
    )

    resolution: bpy.props.EnumProperty(
        name="Resolution",
        items=[
            ('1024', '1024', ''),
            ('2048', '2048', ''),
            ('4096', '4096', ''),
        ],
        default='2048',
        update=update_render_resolution,
    )

    plane_size: bpy.props.FloatProperty(
        name="Plane Size",
        default=2.0,
        min=0.01,
    )

    camera_z_distance: bpy.props.FloatProperty(
        name="Camera Distance",
        default=3.0,
        min=0.1,
    )

    setup_mode: bpy.props.EnumProperty(
        name="Setup Mode",
        items=[
            (MODE_FRONT, "Front", "Vertical front-facing plane with camera looking straight at it"),
            (MODE_TOP_DOWN, "Top-Down", "Horizontal plane with camera looking down"),
        ],
        default=MODE_FRONT,
    )

    transparent_bg: bpy.props.BoolProperty(
        name="Transparent Background",
        default=False,
    )

    use_gpu: bpy.props.BoolProperty(
        name="Use GPU if Available",
        default=True,
    )

    clear_scene_on_setup: bpy.props.BoolProperty(
        name="Clear Scene on Setup",
        default=True,
    )

    switch_to_shading_on_setup: bpy.props.BoolProperty(
        name="Open Shading Workspace",
        default=True,
    )

    camera_view_on_setup: bpy.props.BoolProperty(
        name="Camera View + Rendered",
        default=True,
    )

    depth_front_range: bpy.props.FloatProperty(
        name="Depth Front Range",
        description="Meters toward the camera from the base plane. Export uses a symmetric range around the base plane so the base plane remains 0.5",
        default=0.50,
        min=0.001,
        precision=3,
    )

    depth_back_range: bpy.props.FloatProperty(
        name="Depth Back Range",
        description="Meters behind the base plane. Export uses a symmetric range around the base plane so the base plane remains 0.5",
        default=0.50,
        min=0.001,
        precision=3,
    )

    depth_blur: bpy.props.IntProperty(
        name="Depth Blur",
        description="Pixel blur applied after fixed depth remap",
        default=2,
        min=0,
        max=128,
    )

    show_update_changelog: bpy.props.BoolProperty(
        name="Show Changelog",
        default=False,
    )

    show_reference: bpy.props.BoolProperty(
        name="Show Reference",
        default=False,
    )


    addon_update_json_url: bpy.props.StringProperty(
        name="Update JSON URL",
        description="Raw GitHub URL to version.json",
        default=UPDATE_VERSION_URL,
    )

    addon_local_version: bpy.props.StringProperty(
        name="Current Version",
        default="0.6.10",
    )

    addon_available_version: bpy.props.StringProperty(
        name="Available Version",
        default="",
    )

    addon_download_url: bpy.props.StringProperty(
        name="Download URL",
        default="",
    )

    addon_update_status: bpy.props.StringProperty(
        name="Update Status",
        default="Not checked",
    )

    addon_update_changelog: bpy.props.StringProperty(
        name="Changelog",
        default="",
    )

    addon_update_available: bpy.props.BoolProperty(
        name="Update Available",
        default=False,
    )


# -----------------------------------------------------------------------------
# Setup operator helper
# -----------------------------------------------------------------------------

def create_setup(context, mode):
    scene = context.scene
    props = scene.hl2remaster_props
    props.setup_mode = mode

    if props.save_blend_before_setup:
        ok, message = save_blend_for_texture(props)
        if not ok:
            raise RuntimeError(message)

    current_mat = get_working_material(props)

    if props.clear_scene_on_setup and not props.preserve_node_tree_on_setup:
        clear_scene_objects()
        purge_orphan_data()

    col = ensure_collection(ADDON_COLLECTION_NAME)
    setup_scene(scene, props, preview=True)

    plane = get_or_create_plane(col, props.plane_size, mode)
    cam = get_or_create_camera(col, props.plane_size, props.camera_z_distance, mode)

    if props.preserve_node_tree_on_setup and current_mat is not None:
        mat = rename_working_material_to_texture(current_mat, props)
        update_old_base_image_in_material(mat, props.source_texture)
    else:
        mat = create_material_template(props.source_texture, props.texture_name)

    assign_material(plane, mat)
    scene.camera = cam

    if props.use_source_as_camera_background:
        set_camera_background_from_source(scene, props)

    create_or_update_old_map_plane(props, create_if_missing=False)
    apply_displacement_only_to_all_materials()

    if props.switch_to_shading_on_setup or props.camera_view_on_setup:
        switch_to_shading_and_camera(cam)

    return plane, cam


# -----------------------------------------------------------------------------
# Operators
# -----------------------------------------------------------------------------

class HL2REM_OT_create_front_setup(bpy.types.Operator):
    bl_idname = "hl2remaster.create_front_setup"
    bl_label = "Create Front Setup"
    bl_description = "Create vertical front-facing orthographic setup"

    def invoke(self, context, event):
        props = context.scene.hl2remaster_props
        if props.save_blend_before_setup:
            return context.window_manager.invoke_props_dialog(self, width=420)
        return self.execute(context)

    def draw(self, context):
        props = context.scene.hl2remaster_props
        layout = self.layout
        layout.label(text="Save and continue with Front Setup?")
        layout.label(text=f"Texture: {props.texture_name}")
        layout.label(text=f"Target: {suggested_blend_path(props) or 'Set Working Blend Folder'}")

    def execute(self, context):
        try:
            create_setup(context, MODE_FRONT)
        except Exception as error:
            self.report({'ERROR'}, str(error))
            return {'CANCELLED'}
        self.report({'INFO'}, "Front setup created")
        return {'FINISHED'}


class HL2REM_OT_create_top_down_setup(bpy.types.Operator):
    bl_idname = "hl2remaster.create_top_down_setup"
    bl_label = "Create Top-Down Setup"
    bl_description = "Create horizontal top-down orthographic setup"

    def invoke(self, context, event):
        props = context.scene.hl2remaster_props
        if props.save_blend_before_setup:
            return context.window_manager.invoke_props_dialog(self, width=420)
        return self.execute(context)

    def draw(self, context):
        props = context.scene.hl2remaster_props
        layout = self.layout
        layout.label(text="Save and continue with Top-Down Setup?")
        layout.label(text=f"Texture: {props.texture_name}")
        layout.label(text=f"Target: {suggested_blend_path(props) or 'Set Working Blend Folder'}")

    def execute(self, context):
        try:
            create_setup(context, MODE_TOP_DOWN)
        except Exception as error:
            self.report({'ERROR'}, str(error))
            return {'CANCELLED'}
        self.report({'INFO'}, "Top-Down setup created")
        return {'FINISHED'}


class HL2REM_OT_use_old_base_preview(bpy.types.Operator):
    bl_idname = "hl2remaster.use_old_base_preview"
    bl_label = "Use OLD Base"

    def execute(self, context):
        ok, message = set_old_new_switch(0.0)
        self.report({'INFO'} if ok else {'ERROR'}, "Using OLD Base Color with Diffuse BSDF" if ok else message)
        return {'FINISHED'} if ok else {'CANCELLED'}


class HL2REM_OT_use_new_base_preview(bpy.types.Operator):
    bl_idname = "hl2remaster.use_new_base_preview"
    bl_label = "Use NEW PBR"

    def execute(self, context):
        ok, message = set_old_new_switch(1.0)
        self.report({'INFO'} if ok else {'ERROR'}, "Using NEW PBR material with displacement reconnected" if ok else message)
        return {'FINISHED'} if ok else {'CANCELLED'}


class HL2REM_OT_render_preview(bpy.types.Operator):
    bl_idname = "hl2remaster.render_preview"
    bl_label = "Render Preview"

    def execute(self, context):
        scene = context.scene
        props = scene.hl2remaster_props

        if not props.output_folder:
            self.report({'ERROR'}, "Please set an output folder")
            return {'CANCELLED'}

        path = out_path(props.output_folder, props.texture_name, "preview", "png")
        settings = save_render_settings(scene)

        try:
            setup_scene(scene, props, preview=True)

            if props.use_source_as_camera_background:
                set_camera_background_from_source(scene, props)
                set_camera_background_opacity(scene, props.camera_bg_opacity)

            scene.render.filepath = path
            scene.render.image_settings.file_format = 'PNG'
            scene.render.image_settings.color_mode = 'RGB'
            scene.render.image_settings.color_depth = '8'

            bpy.ops.render.render(write_still=True)
            restore_render_settings(scene, settings)

            self.report({'INFO'}, f"Preview rendered: {path}")
            return {'FINISHED'}

        except Exception as error:
            restore_render_settings(scene, settings)
            self.report({'ERROR'}, str(error))
            return {'CANCELLED'}


class HL2REM_OT_fill_principled(bpy.types.Operator, ImportHelper):
    bl_idname = "hl2remaster.fill_principled"
    bl_label = "Fill Principled From Textures"
    bl_description = "Select multiple PBR textures and connect them to the NEW Principled shader"

    filename_ext = ""

    filter_glob: StringProperty(
        default="*.png;*.jpg;*.jpeg;*.tif;*.tiff;*.exr;*.webp",
        options={'HIDDEN'},
    )

    files: CollectionProperty(
        type=bpy.types.OperatorFileListElement,
        options={'HIDDEN', 'SKIP_SAVE'},
    )

    directory: StringProperty(
        subtype='DIR_PATH',
    )

    def execute(self, context):
        props = context.scene.hl2remaster_props

        paths = [
            os.path.join(self.directory, file.name)
            for file in self.files
        ]

        if not paths and self.filepath:
            paths = [self.filepath]

        ok, message = fill_principled_from_files(props, paths)

        self.report({'INFO'} if ok else {'ERROR'}, message)
        return {'FINISHED'} if ok else {'CANCELLED'}


class HL2REM_OT_export_basecolor(bpy.types.Operator):
    bl_idname = "hl2remaster.export_basecolor"
    bl_label = "Export BaseColor"
    bl_description = "Export BaseColor as a technical emission render from all visible geometry"

    def execute(self, context):
        props = context.scene.hl2remaster_props

        if not props.output_folder:
            self.report({'ERROR'}, "Please set an output folder")
            return {'CANCELLED'}

        ok, message = render_technical_channel(context.scene, props, "BaseColor")
        self.report({'INFO'} if ok else {'ERROR'}, f"BaseColor exported: {message}" if ok else message)
        return {'FINISHED'} if ok else {'CANCELLED'}


class HL2REM_OT_export_roughness(bpy.types.Operator):
    bl_idname = "hl2remaster.export_roughness"
    bl_label = "Export Roughness"
    bl_description = "Export Roughness as a technical emission render from all visible geometry"

    def execute(self, context):
        props = context.scene.hl2remaster_props

        if not props.output_folder:
            self.report({'ERROR'}, "Please set an output folder")
            return {'CANCELLED'}

        ok, message = render_technical_channel(context.scene, props, "Roughness")
        self.report({'INFO'} if ok else {'ERROR'}, f"Roughness exported: {message}" if ok else message)
        return {'FINISHED'} if ok else {'CANCELLED'}


class HL2REM_OT_export_metallic(bpy.types.Operator):
    bl_idname = "hl2remaster.export_metallic"
    bl_label = "Export Metallic"
    bl_description = "Export Metallic as a technical emission render from all visible geometry"

    def execute(self, context):
        props = context.scene.hl2remaster_props

        if not props.output_folder:
            self.report({'ERROR'}, "Please set an output folder")
            return {'CANCELLED'}

        ok, message = render_technical_channel(context.scene, props, "Metallic")
        self.report({'INFO'} if ok else {'ERROR'}, f"Metallic exported: {message}" if ok else message)
        return {'FINISHED'} if ok else {'CANCELLED'}


class HL2REM_OT_export_normal(bpy.types.Operator):
    bl_idname = "hl2remaster.export_normal"
    bl_label = "Export Normal"
    bl_description = "Export Normal. Front setup converts Normal Pass into front-facing texture normal."

    def execute(self, context):
        props = context.scene.hl2remaster_props

        if not props.output_folder:
            self.report({'ERROR'}, "Please set an output folder")
            return {'CANCELLED'}

        ok, message = export_normal_pass(context.scene, props)
        self.report({'INFO'} if ok else {'ERROR'}, f"Normal exported: {message}" if ok else message)
        return {'FINISHED'} if ok else {'CANCELLED'}


class HL2REM_OT_setup_depth_pass_viewer(bpy.types.Operator):
    bl_idname = "hl2remaster.setup_depth_pass_viewer"
    bl_label = "Setup Depth Pass Viewer"
    bl_description = "Enable Depth pass and render fixed-range blurred depth viewer"

    def execute(self, context):
        ok, message, img = render_depth_viewer(context.scene, context.scene.hl2remaster_props)

        if ok:
            switch_to_compositing()
            show_image_in_image_editor(img)

        self.report({'INFO'} if ok else {'ERROR'}, message)
        return {'FINISHED'} if ok else {'CANCELLED'}


class HL2REM_OT_export_displacement(bpy.types.Operator):
    bl_idname = "hl2remaster.export_displacement"
    bl_label = "Export Displacement EXR"
    bl_description = "Export Displacement as EXR from fixed-range Depth pass"

    def execute(self, context):
        props = context.scene.hl2remaster_props

        if not props.output_folder:
            self.report({'ERROR'}, "Please set an output folder")
            return {'CANCELLED'}

        ok, message = export_displacement(context.scene, props)
        self.report({'INFO'} if ok else {'ERROR'}, f"Displacement EXR exported: {message}" if ok else message)
        return {'FINISHED'} if ok else {'CANCELLED'}


class HL2REM_OT_test_maps(bpy.types.Operator):
    bl_idname = "hl2remaster.test_maps"
    bl_label = "Test Maps"
    bl_description = "Create a test plane using the exported maps for the current Texture Name"

    def execute(self, context):
        props = context.scene.hl2remaster_props

        if not props.output_folder:
            self.report({'ERROR'}, "Please set an output folder")
            return {'CANCELLED'}

        output_folder = bpy.path.abspath(props.output_folder)
        required_base = os.path.join(output_folder, f"{props.texture_name}_BaseColor.png")

        if not os.path.isfile(required_base):
            self.report({'ERROR'}, f"BaseColor not found: {required_base}")
            return {'CANCELLED'}

        try:
            test_plane = create_or_update_test_maps_plane(props)

            bpy.ops.object.select_all(action='DESELECT')
            test_plane.select_set(True)
            bpy.context.view_layer.objects.active = test_plane

            self.report({'INFO'}, "Test maps plane created")
            return {'FINISHED'}

        except Exception as error:
            self.report({'ERROR'}, f"Could not create test maps plane: {error}")
            return {'CANCELLED'}


class HL2REM_OT_old_map(bpy.types.Operator):
    bl_idname = "hl2remaster.old_map"
    bl_label = "Old Map"
    bl_description = "Create or update a left-side reference plane using the current Source Base Color"

    def execute(self, context):
        props = context.scene.hl2remaster_props
        if not props.source_texture:
            self.report({'ERROR'}, "Please set a Source Base Color")
            return {'CANCELLED'}
        if not os.path.isfile(bpy.path.abspath(props.source_texture)):
            self.report({'ERROR'}, "Source Base Color image not found")
            return {'CANCELLED'}
        try:
            old_plane = create_or_update_old_map_plane(props, create_if_missing=True)
            if old_plane is None:
                self.report({'ERROR'}, "Could not create Old Map plane")
                return {'CANCELLED'}
            bpy.ops.object.select_all(action='DESELECT')
            old_plane.select_set(True)
            bpy.context.view_layer.objects.active = old_plane
            self.report({'INFO'}, "Old Map plane created")
            return {'FINISHED'}
        except Exception as error:
            self.report({'ERROR'}, f"Could not create Old Map plane: {error}")
            return {'CANCELLED'}


class HL2REM_OT_uv_rectify(bpy.types.Operator):
    bl_idname = "hl2remaster.uv_rectify"
    bl_label = "Rectify UV"
    bl_description = "Rectify selected UV faces or vertices. Blender 5 compatible replacement for TexTools Rectify"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        if not hl2_uv_is_uv_editor(context):
            return False

        if not context.active_object:
            return False

        if context.active_object.mode != 'EDIT':
            return False

        if context.active_object.type != 'MESH':
            return False

        if not context.active_object.data.uv_layers:
            return False

        if context.scene.tool_settings.use_uv_select_sync:
            return False

        return True

    def execute(self, context):
        ok, message = hl2_rectify_uv_selection(context)
        self.report({'INFO'} if ok else {'ERROR'}, message)
        return {'FINISHED'} if ok else {'CANCELLED'}



class HL2REM_OT_check_for_updates(bpy.types.Operator):
    bl_idname = "hl2remaster.check_for_updates"
    bl_label = "Check for Updates"
    bl_description = "Check GitHub version.json for a newer addon version"

    def execute(self, context):
        props = context.scene.hl2remaster_props
        props.addon_local_version = get_local_addon_version_string()
        props.addon_update_status = "Checking for updates..."

        ok, message, data = fetch_update_json(props)

        if not ok:
            props.addon_update_status = message
            self.report({'ERROR'}, message)
            return {'CANCELLED'}

        update_addon_update_props(props, data)
        self.report({'INFO'}, props.addon_update_status)
        return {'FINISHED'}


class HL2REM_OT_install_latest_update(bpy.types.Operator):
    bl_idname = "hl2remaster.install_latest_update"
    bl_label = "Install Latest Version"
    bl_description = "Download and replace this addon file with the latest version from GitHub raw"

    def execute(self, context):
        props = context.scene.hl2remaster_props
        download_url = props.addon_download_url

        if not download_url:
            ok, message, data = fetch_update_json(props)

            if not ok:
                props.addon_update_status = message
                self.report({'ERROR'}, message)
                return {'CANCELLED'}

            update_addon_update_props(props, data)
            download_url = props.addon_download_url

        if not download_url:
            props.addon_update_status = "No download URL found in update data"
            self.report({'ERROR'}, props.addon_update_status)
            return {'CANCELLED'}

        local_version = get_local_addon_version_string()
        available_version = props.addon_available_version or "0.0.0"

        if props.addon_available_version and not is_remote_version_newer(local_version, available_version):
            props.addon_update_status = f"Already up to date: {local_version}"
            self.report({'INFO'}, props.addon_update_status)
            return {'FINISHED'}

        ok, message = install_addon_update_from_url(download_url)
        props.addon_update_status = message

        self.report({'INFO'} if ok else {'ERROR'}, message)
        return {'FINISHED'} if ok else {'CANCELLED'}


# -----------------------------------------------------------------------------
# UI
# -----------------------------------------------------------------------------

def draw_hl2_panel(layout, context, compact=False):
    props = context.scene.hl2remaster_props

    setup_box = layout.box()
    setup_box.label(text="Setup")
    setup_box.prop(props, "texture_name")
    setup_box.prop(props, "source_texture")
    setup_box.prop(props, "output_folder")
    setup_box.prop(props, "resolution")
    setup_box.prop(props, "plane_size")
    setup_box.prop(props, "camera_z_distance")
    setup_box.prop(props, "setup_mode")

    setup_box.separator()
    setup_box.prop(props, "clear_scene_on_setup")
    setup_box.prop(props, "save_blend_before_setup")
    setup_box.prop(props, "preserve_node_tree_on_setup")
    setup_box.prop(props, "use_source_as_camera_background")
    setup_box.prop(props, "working_blend_folder")

    setup_box.separator()
    setup_box.operator("hl2remaster.create_front_setup", icon='MOD_BUILD')
    setup_box.operator("hl2remaster.create_top_down_setup", icon='MOD_BUILD')

    fill_box = layout.box()
    fill_box.label(text="Fill Principled")
    fill_box.operator("hl2remaster.fill_principled", text="Fill Principled From Textures", icon='NODE_MATERIAL')

    preview_box = layout.box()
    preview_box.label(text="Preview")

    row = preview_box.row(align=True)
    row.operator("hl2remaster.use_old_base_preview", icon='IMAGE_DATA')
    row.operator("hl2remaster.use_new_base_preview", icon='MATERIAL')

    preview_box.prop(props, "camera_bg_opacity")
    preview_box.operator("hl2remaster.render_preview", icon='RENDER_STILL')

    export_box = layout.box()
    export_box.label(text="PBR Export")
    export_box.operator("hl2remaster.export_basecolor", icon='IMAGE_DATA')
    export_box.operator("hl2remaster.export_roughness", icon='TEXTURE_DATA')
    export_box.operator("hl2remaster.export_metallic", icon='TEXTURE_DATA')
    export_box.operator("hl2remaster.export_normal", icon='NORMALS_FACE')

    depth_box = layout.box()
    depth_box.label(text="Depth / Displacement")
    depth_box.prop(props, "depth_front_range")
    depth_box.prop(props, "depth_back_range")
    depth_box.prop(props, "depth_blur")

    depth_box.operator("hl2remaster.setup_depth_pass_viewer", text="Setup Depth Pass Viewer", icon='NODETREE')
    depth_box.operator("hl2remaster.export_displacement", text="Export Displacement EXR", icon='IMAGE_DATA')

    test_box = layout.box()
    test_box.label(text="Test")
    test_box.operator("hl2remaster.old_map", text="Old Map", icon='IMAGE_DATA')
    test_box.operator("hl2remaster.test_maps", text="Test Maps", icon='MATERIAL')

    uv_box = layout.box()
    uv_box.label(text="UV Tools")
    uv_box.operator("hl2remaster.uv_rectify", text="Rectify UV", icon='UV')

    update_box = layout.box()
    update_box.label(text="Addon Update")
    update_box.label(text=f"Current: {get_local_addon_version_string()}")

    available_version = getattr(props, "addon_available_version", "")
    update_status = getattr(props, "addon_update_status", "Not checked")
    update_changelog = getattr(props, "addon_update_changelog", "")

    if available_version:
        update_box.label(text=f"Available: {available_version}")

    update_box.label(text=update_status)
    update_box.operator("hl2remaster.check_for_updates", text="Check for Updates", icon='FILE_REFRESH')
    update_box.operator("hl2remaster.install_latest_update", text="Install Latest Version", icon='IMPORT')

    if update_changelog:
        changelog_row = update_box.row()
        changelog_row.prop(
            props,
            "show_update_changelog",
            icon='TRIA_DOWN' if props.show_update_changelog else 'TRIA_RIGHT',
            icon_only=True,
            emboss=False,
        )
        changelog_row.label(text="Changelog")

        if props.show_update_changelog:
            for line in update_changelog.splitlines()[:12]:
                update_box.label(text=line)

    if not compact:
        ref = layout.box()
        ref_row = ref.row()
        ref_row.prop(
            props,
            "show_reference",
            icon='TRIA_DOWN' if props.show_reference else 'TRIA_RIGHT',
            icon_only=True,
            emboss=False,
        )
        ref_row.label(text="Reference")

        if props.show_reference:
            ref.label(text="Create Front Setup = vertical wall workflow")
            ref.label(text="Create Top-Down Setup = horizontal legacy workflow")
            ref.label(text="Normal adapts to setup mode")
            ref.label(text="Front normal: R=X, G=Z, B=-Y")
            ref.label(text="Old Map = left-side source reference")
            ref.label(text="Old/Test planes gap = 0.20m")
            ref.label(text="Test Maps displacement scale = 1.0")
            ref.label(text="Base plane maps to 0.5")
            ref.label(text="Depth uses symmetric max(front, back)")
            ref.label(text="UV Rectify requires UV Editor, Edit Mode, Sync Off")
            ref.label(text="Dicing Render = 1")
            ref.label(text="Dicing Viewport = 1")
            ref.label(text="Addon updates use GitHub raw version.json")


class HL2REM_PT_panel(bpy.types.Panel):
    bl_label = "HL2 Remaster"
    bl_idname = "HL2REM_PT_panel"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "HL2 Remaster"

    def draw(self, context):
        draw_hl2_panel(self.layout, context, compact=False)


class HL2REM_PT_image_editor_panel(bpy.types.Panel):
    bl_label = "HL2 Remaster"
    bl_idname = "HL2REM_PT_image_editor_panel"
    bl_space_type = 'IMAGE_EDITOR'
    bl_region_type = 'UI'
    bl_category = "HL2 Remaster"

    def draw(self, context):
        draw_hl2_panel(self.layout, context, compact=True)


classes = (
    HL2RemasterProperties,
    HL2REM_OT_create_front_setup,
    HL2REM_OT_create_top_down_setup,
    HL2REM_OT_use_old_base_preview,
    HL2REM_OT_use_new_base_preview,
    HL2REM_OT_render_preview,
    HL2REM_OT_fill_principled,
    HL2REM_OT_export_basecolor,
    HL2REM_OT_export_roughness,
    HL2REM_OT_export_metallic,
    HL2REM_OT_export_normal,
    HL2REM_OT_setup_depth_pass_viewer,
    HL2REM_OT_export_displacement,
    HL2REM_OT_test_maps,
    HL2REM_OT_old_map,
    HL2REM_OT_uv_rectify,
    HL2REM_OT_check_for_updates,
    HL2REM_OT_install_latest_update,
    HL2REM_PT_panel,
    HL2REM_PT_image_editor_panel,
)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)

    bpy.types.Scene.hl2remaster_props = bpy.props.PointerProperty(type=HL2RemasterProperties)


def unregister():
    if hasattr(bpy.types.Scene, "hl2remaster_props"):
        del bpy.types.Scene.hl2remaster_props

    for cls in reversed(classes):
        try:
            bpy.utils.unregister_class(cls)
        except Exception:
            pass


if __name__ == "__main__":
    register()