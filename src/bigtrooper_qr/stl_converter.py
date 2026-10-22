"""Convert QR code images to 3D STL and 3MF models.

This module handles the conversion of QR code images to 3D printable formats.
It supports two different output formats with different geometry generation approaches:

STL Format:
- Optimized for single-material 3D printing
- Uses an efficient surface-based approach
- Generates only the visible surfaces (top surfaces + walls)
- Internal walls ensure proper slicing for 3D printing
- Results in smaller file sizes and faster processing

3MF Format:
- Designed for multi-material/multi-color 3D printing
- Uses complete 3D object generation
- Each QR module is a full 3D box with all 6 faces
- Base plate is a complete 3D box
- Supports color/material assignment per component
- Results in larger files but enables color printing

The different approaches are necessary because:
- STL format only contains geometry, no color information
- 3MF format embeds color/material data with the geometry
- Slicers handle the formats differently for multi-material printing
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image, ImageDraw
from stl import mesh  # type: ignore[import-untyped]

try:
    import lib3mf
    from lib3mf import get_wrapper

    HAS_LIB3MF = True
except ImportError:
    HAS_LIB3MF = False

from .logging_config import get_logger
from .utils import (
    color_to_3mf_format,
    ensure_grayscale,
    load_font_with_fallbacks,
    numpy_mesh_to_trimesh,
    parse_color,
    prepare_output_path,
    repair_mesh,
    save_mesh_debug_view,
    validate_mesh,
)

logger = get_logger(__name__)

# Keychain tab outline resolution: evenly spaced samples plus the two square corners
KEYCHAIN_SEGMENTS = 32
# Top, bottom, outer wall and hole wall each use 2 triangles per outline point
KEYCHAIN_TRIANGLE_COUNT = 8 * (KEYCHAIN_SEGMENTS + 2)

# Minimum clearance between back text and the edge of the base
BACK_TEXT_MARGIN_MM = 1.5


def render_back_text_mask(
    text: str,
    grid_shape: tuple[int, int],
    pixel_size: float,
    text_size_mm: float = 4.0,
) -> np.ndarray:
    """Render text as a mask for recessing into the bottom face of the model.

    The text is centered on the base and mirrored so it reads correctly when the
    model is flipped over. If it does not fit at the requested size it is shrunk.

    Args:
        text: Text to render. Lines are separated by newlines or a literal "\\n".
        grid_shape: (rows, cols) of the height map the mask must match
        pixel_size: Size of each height map pixel in mm
        text_size_mm: Target height of capital letters in mm

    Returns:
        Boolean array with the height map's shape, True where text is recessed

    Raises:
        ValueError: If the text is empty or cannot fit on the base
    """
    lines = [line.strip() for line in text.replace("\\n", "\n").split("\n") if line.strip()]
    if not lines:
        raise ValueError("Back text is empty")
    joined = "\n".join(lines)

    rows, cols = grid_shape
    margin_px = max(1, math.ceil(BACK_TEXT_MARGIN_MM / pixel_size))
    max_width = cols - 2 * margin_px
    max_height = rows - 2 * margin_px

    # Measure how tall capital letters are relative to the font size
    draw_dummy = ImageDraw.Draw(Image.new("L", (1, 1)))
    ref_font = load_font_with_fallbacks(font_size=100, use_bold=True, text=joined)
    ref_bbox = draw_dummy.textbbox((0, 0), "H", font=ref_font)
    cap_ratio = (ref_bbox[3] - ref_bbox[1]) / 100

    requested_size = max(1, round(text_size_mm / pixel_size / cap_ratio))
    font_size = requested_size
    while True:
        font = load_font_with_fallbacks(font_size=font_size, use_bold=True, text=joined)
        spacing = max(1, round(font_size * 0.25))
        bbox = draw_dummy.multiline_textbbox((0, 0), joined, font=font, spacing=spacing, align="center")
        text_width = bbox[2] - bbox[0]
        text_height = bbox[3] - bbox[1]
        if text_width <= max_width and text_height <= max_height:
            break
        if font_size <= 4:
            raise ValueError(f"Back text '{joined}' does not fit on the base. Use shorter text or a larger base.")
        font_size = max(4, int(font_size * 0.9))

    if font_size < requested_size:
        logger.warning(
            f"Back text shrunk to fit: letters are {font_size * cap_ratio * pixel_size:.1f}mm tall "
            f"instead of {text_size_mm:.1f}mm"
        )

    text_image = Image.new("L", (cols, rows), 255)
    draw = ImageDraw.Draw(text_image)
    text_x = (cols - text_width) / 2 - bbox[0]
    text_y = (rows - text_height) / 2 - bbox[1]
    draw.multiline_text((text_x, text_y), joined, fill=0, font=font, spacing=spacing, align="center")

    # Binarize like front labels, then match the height map's vertical flip and
    # mirror horizontally so the text reads correctly from underneath
    mask = np.array(text_image) <= 128
    return np.fliplr(np.flipud(mask))


def image_to_3d_array(
    image: Image.Image,
    base_height: float = 2.0,
    qr_height: float = 2.0,
    invert: bool = False,
) -> np.ndarray:
    """Convert a QR code image to a 3D height map array.

    Args:
        image: PIL Image of the QR code
        base_height: Height of the solid base plate in mm
        qr_height: Additional height for QR code modules in mm (black areas extend this much above base)
        invert: If True, black areas are recessed and white areas are raised

    Returns:
        3D numpy array representing heights from z=0 with solid base
    """
    # Convert to grayscale if needed
    image = ensure_grayscale(image)

    # Convert to numpy array
    img_array = np.array(image)

    # Normalize to 0-1
    img_array = img_array / 255.0

    # Invert if requested
    if invert:
        img_array = 1.0 - img_array

    # Create height map
    # All areas have at least base_height (solid base plate)
    # Black pixels (0) get additional qr_height on top of base
    # White pixels (1) stay at base height
    # This creates a solid base with QR pattern extruded on top
    height_map = np.full_like(img_array, base_height, dtype=float)
    # Add QR height to black areas (where img_array is 0)
    height_map = height_map + (1.0 - img_array) * qr_height

    return height_map


def image_to_multilayer_3d_array(
    image: Image.Image,
    layer_heights: list[float],
    invert: bool = False,
    has_frame: bool = False,
) -> np.ndarray:
    """Convert a QR code image to a multi-layer 3D height map array.

    Args:
        image: PIL Image of the QR code (may include frame)
        layer_heights: List of heights [base, qr, frame] in mm
        invert: If True, black areas are recessed and white areas are raised
        has_frame: If True, outer border pixels are treated as frame

    Returns:
        3D numpy array representing heights with multiple distinct layers
    """
    # Convert to grayscale if needed
    image = ensure_grayscale(image)

    # Convert to numpy array
    img_array = np.array(image)
    height, width = img_array.shape

    # Normalize to 0-255 range
    img_array = img_array.astype(float)

    # Create height map starting with base height
    height_map = np.full_like(img_array, layer_heights[0], dtype=float)

    if has_frame and len(layer_heights) >= 3:
        # For frames, we need a more sophisticated approach
        # The frame is typically added as a border around the QR code
        # We'll detect it by looking for connected black regions from the edges

        # Normalize to 0-1 range
        img_normalized = img_array / 255.0

        # First, identify all black pixels
        black_mask = img_normalized < 0.5

        # Import scipy for connected component analysis
        from scipy import ndimage

        # Create frame mask - black pixels that are connected to the image border
        frame_mask = np.zeros_like(img_array, dtype=bool)

        # Use flood fill from edges to find frame regions
        # Start with edge pixels
        edge_mask = np.zeros_like(img_array, dtype=bool)
        edge_mask[0, :] = True  # Top edge
        edge_mask[-1, :] = True  # Bottom edge
        edge_mask[:, 0] = True  # Left edge
        edge_mask[:, -1] = True  # Right edge

        # Find black pixels on edges
        edge_black = edge_mask & black_mask

        if np.any(edge_black):
            # Label all black connected components
            structure = np.ones((3, 3), dtype=bool)  # 8-connectivity
            labeled, num_features = ndimage.label(black_mask, structure=structure)  # type: ignore[misc]

            # Get labels of components touching edges
            edge_labels = np.unique(labeled[edge_black])
            edge_labels = edge_labels[edge_labels != 0]  # Remove background label

            # Create frame mask from edge-connected components
            for label in edge_labels:
                component_mask = labeled == label
                # Check if this component forms a frame-like structure
                # (touches multiple edges and forms a border)
                touches_top = np.any(component_mask[0, :])
                touches_bottom = np.any(component_mask[-1, :])
                touches_left = np.any(component_mask[:, 0])
                touches_right = np.any(component_mask[:, -1])

                # A frame should touch at least 3 edges or form a continuous border
                edge_count = touches_top + touches_bottom + touches_left + touches_right
                if edge_count >= 3:
                    # Additional check: frame components should be relatively large
                    component_size = np.sum(component_mask)
                    total_black = np.sum(black_mask)
                    # Frame should be at least 10% of black pixels
                    if component_size > 0.1 * total_black:
                        frame_mask |= component_mask

        # Apply heights based on the masks
        # Frame gets the highest layer
        height_map[frame_mask] = layer_heights[2]

        # QR modules (black pixels not in frame) get middle layer
        qr_mask = black_mask & ~frame_mask
        height_map[qr_mask] = layer_heights[1]

        # White areas stay at base height (already set)

        # Handle inverted case
        if invert:
            # Swap QR and base heights
            white_mask = ~black_mask
            height_map[white_mask & ~frame_mask] = layer_heights[1]
            height_map[qr_mask] = layer_heights[0]

    else:
        # No frame, process entire image as QR code
        img_normalized = img_array / 255.0

        if invert:
            img_normalized = 1.0 - img_normalized

        # Black pixels (0) get QR height, white pixels stay at base
        height_map = np.where(img_normalized < 0.5, layer_heights[1], layer_heights[0])

    return height_map


def create_stl_from_heightmap(
    height_map: np.ndarray,
    pixel_size: float = 1.0,
    mount_type: str | None = None,
    hole_diameter: float = 4.0,
    validate_and_repair: bool = True,
    debug_path: Path | str | None = None,
    bottom_map: np.ndarray | None = None,
) -> Any:
    """Create an STL mesh from a height map.

    Args:
        height_map: 2D array of heights
        pixel_size: Size of each pixel in mm
        mount_type: Type of mounting feature to add
        hole_diameter: Diameter of mounting holes in mm
        validate_and_repair: If True, validate and repair the mesh
        debug_path: If provided, save debug visualization to this path
        bottom_map: Optional 2D array of bottom face heights, same shape as height_map.
            Values above 0 are recessed into the bottom (used for back text). The outer
            border must stay at 0.

    Returns:
        STL Mesh object with optional mounting features
    """
    height, width = height_map.shape

    # We need to count faces more carefully
    # Top surface: 2 triangles per pixel
    num_faces = height * width * 2

    if bottom_map is None:
        # Bottom surface: 2 triangles for entire base
        num_faces += 2
    else:
        # Bottom surface: 2 triangles per pixel plus walls at each recess edge
        num_faces += height * width * 2
        num_faces += 2 * int(np.count_nonzero(bottom_map[:, 1:] != bottom_map[:, :-1]))
        num_faces += 2 * int(np.count_nonzero(bottom_map[1:, :] != bottom_map[:-1, :]))

    # Outer walls: 2 triangles per edge pixel
    num_faces += 2 * (2 * height + 2 * width)

    # Internal walls: We need walls at every height transition
    # Count transitions between adjacent pixels
    for y in range(height):
        for x in range(width):
            curr_height = height_map[y, x]
            # Check right neighbor
            if x < width - 1 and height_map[y, x + 1] != curr_height:
                num_faces += 2  # Two triangles for vertical wall
            # Check bottom neighbor
            if y < height - 1 and height_map[y + 1, x] != curr_height:
                num_faces += 2  # Two triangles for vertical wall

    # Add faces for mounting features
    if mount_type == "keychain":
        num_faces += KEYCHAIN_TRIANGLE_COUNT

    # Create mesh
    stl_mesh: Any = mesh.Mesh(np.zeros(num_faces, dtype=mesh.Mesh.dtype))

    face_idx = 0

    # Create top surface
    for y in range(height):
        for x in range(width):
            # Get the four corners of this pixel
            x0, y0 = x * pixel_size, y * pixel_size
            x1, y1 = (x + 1) * pixel_size, (y + 1) * pixel_size

            # Height for this pixel
            h = height_map[y, x]

            # First triangle
            stl_mesh.vectors[face_idx] = np.array([[x0, y0, h], [x1, y0, h], [x0, y1, h]])
            face_idx += 1

            # Second triangle
            stl_mesh.vectors[face_idx] = np.array([[x1, y0, h], [x1, y1, h], [x0, y1, h]])
            face_idx += 1

    # Create internal walls at height transitions
    for y in range(height):
        for x in range(width):
            x0, y0 = x * pixel_size, y * pixel_size
            x1, y1 = (x + 1) * pixel_size, (y + 1) * pixel_size
            curr_height = height_map[y, x]

            # Check right neighbor - create vertical wall if heights differ
            if x < width - 1:
                right_height = height_map[y, x + 1]
                if curr_height != right_height:
                    # Create wall between current and right pixel
                    # Wall goes from min height to max height
                    min_h = min(curr_height, right_height)
                    max_h = max(curr_height, right_height)

                    # Two triangles forming a vertical rectangle
                    if curr_height > right_height:
                        # Current is higher, normal points left
                        stl_mesh.vectors[face_idx] = np.array([[x1, y0, min_h], [x1, y1, min_h], [x1, y0, max_h]])
                        face_idx += 1

                        stl_mesh.vectors[face_idx] = np.array([[x1, y1, min_h], [x1, y1, max_h], [x1, y0, max_h]])
                        face_idx += 1
                    else:
                        # Right is higher, normal points right
                        stl_mesh.vectors[face_idx] = np.array([[x1, y0, min_h], [x1, y0, max_h], [x1, y1, min_h]])
                        face_idx += 1

                        stl_mesh.vectors[face_idx] = np.array([[x1, y0, max_h], [x1, y1, max_h], [x1, y1, min_h]])
                        face_idx += 1

            # Check bottom neighbor - create vertical wall if heights differ
            if y < height - 1:
                bottom_height = height_map[y + 1, x]
                if curr_height != bottom_height:
                    # Create wall between current and bottom pixel
                    # Wall goes from min height to max height
                    min_h = min(curr_height, bottom_height)
                    max_h = max(curr_height, bottom_height)

                    # Two triangles forming a vertical rectangle
                    if curr_height > bottom_height:
                        # Current is higher, normal points up
                        stl_mesh.vectors[face_idx] = np.array([[x0, y1, min_h], [x1, y1, min_h], [x0, y1, max_h]])
                        face_idx += 1

                        stl_mesh.vectors[face_idx] = np.array([[x1, y1, min_h], [x1, y1, max_h], [x0, y1, max_h]])
                        face_idx += 1
                    else:
                        # Bottom is higher, normal points down
                        stl_mesh.vectors[face_idx] = np.array([[x0, y1, min_h], [x0, y1, max_h], [x1, y1, min_h]])
                        face_idx += 1

                        stl_mesh.vectors[face_idx] = np.array([[x0, y1, max_h], [x1, y1, max_h], [x1, y1, min_h]])
                        face_idx += 1

    # Create outer walls
    total_width = width * pixel_size
    total_height = height * pixel_size

    # Front side (y=0)
    for x in range(width):
        x0, x1 = x * pixel_size, (x + 1) * pixel_size
        h = height_map[0, x]

        # Two triangles
        stl_mesh.vectors[face_idx] = np.array([[x0, 0, 0], [x1, 0, 0], [x0, 0, h]])
        face_idx += 1
        stl_mesh.vectors[face_idx] = np.array([[x1, 0, 0], [x1, 0, h], [x0, 0, h]])
        face_idx += 1

    # Back side (y=max)
    for x in range(width):
        x0, x1 = x * pixel_size, (x + 1) * pixel_size
        h = height_map[height - 1, x]

        # Two triangles
        stl_mesh.vectors[face_idx] = np.array([[x0, total_height, h], [x1, total_height, h], [x0, total_height, 0]])
        face_idx += 1
        stl_mesh.vectors[face_idx] = np.array([[x1, total_height, h], [x1, total_height, 0], [x0, total_height, 0]])
        face_idx += 1

    # Left side (x=0)
    for y in range(height):
        y0, y1 = y * pixel_size, (y + 1) * pixel_size
        h = height_map[y, 0]

        # Two triangles
        stl_mesh.vectors[face_idx] = np.array([[0, y0, h], [0, y1, h], [0, y0, 0]])
        face_idx += 1
        stl_mesh.vectors[face_idx] = np.array([[0, y1, h], [0, y1, 0], [0, y0, 0]])
        face_idx += 1

    # Right side (x=max)
    for y in range(height):
        y0, y1 = y * pixel_size, (y + 1) * pixel_size
        h = height_map[y, width - 1]

        # Two triangles
        stl_mesh.vectors[face_idx] = np.array([[total_width, y0, 0], [total_width, y1, 0], [total_width, y0, h]])
        face_idx += 1
        stl_mesh.vectors[face_idx] = np.array([[total_width, y1, 0], [total_width, y1, h], [total_width, y0, h]])
        face_idx += 1

    if bottom_map is None:
        # Bottom face - solid base covering entire area
        stl_mesh.vectors[face_idx] = np.array([[0, 0, 0], [total_width, 0, 0], [0, total_height, 0]])
        face_idx += 1
        stl_mesh.vectors[face_idx] = np.array(
            [[total_width, 0, 0], [total_width, total_height, 0], [0, total_height, 0]]
        )
        face_idx += 1
    else:
        face_idx = add_recessed_bottom(stl_mesh, face_idx, bottom_map, pixel_size)

    # Add mounting features if requested
    if mount_type == "keychain":
        # Get the maximum height of the model
        max_height = np.max(height_map)
        face_idx = add_keychain_mount(
            stl_mesh,
            face_idx,
            total_width,
            total_height,
            max_height,
            hole_diameter,
        )

    # Update normals
    stl_mesh.update_normals()

    logger.info(f"Created STL mesh with {face_idx} faces")

    # Validate and repair if requested
    if validate_and_repair:
        logger.debug("Validating mesh...")

        # Convert numpy-stl mesh to trimesh for validation
        vertices = stl_mesh.vectors.reshape(-1, 3)
        faces = np.arange(len(vertices)).reshape(-1, 3)
        trimesh_obj = numpy_mesh_to_trimesh(vertices, faces)

        # Validate mesh
        validation_results = validate_mesh(trimesh_obj, verbose=False)

        if not validation_results["is_watertight"] or validation_results["duplicate_faces"] > 0:
            logger.warning(
                f"Mesh issues detected - Watertight: {validation_results['is_watertight']}, "
                f"Duplicate faces: {validation_results['duplicate_faces']}, "
                f"Non-manifold edges: {validation_results['non_manifold_edges']}"
            )

            # Repair the mesh
            logger.info("Repairing mesh...")
            repaired_mesh = repair_mesh(trimesh_obj, verbose=True)

            # Convert back to numpy-stl format
            repaired_vertices = repaired_mesh.vertices[repaired_mesh.faces].reshape(-1, 3, 3)
            stl_mesh = mesh.Mesh(np.zeros(len(repaired_vertices), dtype=mesh.Mesh.dtype))
            stl_mesh.vectors = repaired_vertices
            stl_mesh.update_normals()

            # Re-validate
            final_validation = validate_mesh(repaired_mesh, verbose=False)
            logger.info(
                f"Repair complete - Watertight: {final_validation['is_watertight']}, "
                f"Faces: {final_validation['face_count']}"
            )
        else:
            logger.debug("Mesh validation passed")

        # Save debug view if requested
        if debug_path:
            logger.info(f"Saving debug view to {debug_path}")
            save_mesh_debug_view(trimesh_obj, debug_path)

    return stl_mesh


def add_recessed_bottom(stl_mesh: Any, face_idx: int, bottom_map: np.ndarray, pixel_size: float) -> int:
    """Add a bottom surface with recesses (such as back text) to an STL mesh.

    Args:
        stl_mesh: The STL mesh object to add faces to
        face_idx: Current face index to start adding faces
        bottom_map: 2D array of bottom face heights; 0 is flush with the print bed
        pixel_size: Size of each pixel in mm

    Returns:
        Updated face index after adding bottom faces
    """
    height, width = bottom_map.shape

    for y in range(height):
        for x in range(width):
            x0, y0 = x * pixel_size, y * pixel_size
            x1, y1 = (x + 1) * pixel_size, (y + 1) * pixel_size
            b = bottom_map[y, x]

            # Floor of this pixel, facing down
            stl_mesh.vectors[face_idx] = np.array([[x0, y0, b], [x0, y1, b], [x1, y0, b]])
            face_idx += 1
            stl_mesh.vectors[face_idx] = np.array([[x1, y0, b], [x0, y1, b], [x1, y1, b]])
            face_idx += 1

            # Wall to the right neighbor, facing into whichever side is recessed deeper
            if x < width - 1 and bottom_map[y, x + 1] != b:
                right_b = bottom_map[y, x + 1]
                lo, hi = min(b, right_b), max(b, right_b)
                if b > right_b:
                    stl_mesh.vectors[face_idx] = np.array([[x1, y0, lo], [x1, y0, hi], [x1, y1, lo]])
                    stl_mesh.vectors[face_idx + 1] = np.array([[x1, y0, hi], [x1, y1, hi], [x1, y1, lo]])
                else:
                    stl_mesh.vectors[face_idx] = np.array([[x1, y0, lo], [x1, y1, lo], [x1, y0, hi]])
                    stl_mesh.vectors[face_idx + 1] = np.array([[x1, y1, lo], [x1, y1, hi], [x1, y0, hi]])
                face_idx += 2

            # Wall to the next row, facing into whichever side is recessed deeper
            if y < height - 1 and bottom_map[y + 1, x] != b:
                next_b = bottom_map[y + 1, x]
                lo, hi = min(b, next_b), max(b, next_b)
                if b > next_b:
                    stl_mesh.vectors[face_idx] = np.array([[x0, y1, lo], [x1, y1, lo], [x0, y1, hi]])
                    stl_mesh.vectors[face_idx + 1] = np.array([[x1, y1, lo], [x1, y1, hi], [x0, y1, hi]])
                else:
                    stl_mesh.vectors[face_idx] = np.array([[x0, y1, lo], [x0, y1, hi], [x1, y1, lo]])
                    stl_mesh.vectors[face_idx + 1] = np.array([[x0, y1, hi], [x1, y1, hi], [x1, y1, lo]])
                face_idx += 2

    return face_idx


def generate_box_geometry(
    x0: float, y0: float, z0: float, x1: float, y1: float, z1: float
) -> tuple[list[list[float]], list[tuple[int, int, int]]]:
    """Generate vertices and triangles for a 3D box.

    Args:
        x0, y0, z0: Minimum coordinates
        x1, y1, z1: Maximum coordinates

    Returns:
        Tuple of (vertices, triangles) where:
        - vertices is a list of [x, y, z] coordinates
        - triangles is a list of (v0, v1, v2) vertex indices
    """
    vertices = [
        [x0, y0, z0],  # 0: bottom-left-bottom
        [x1, y0, z0],  # 1: bottom-right-bottom
        [x1, y1, z0],  # 2: top-right-bottom
        [x0, y1, z0],  # 3: top-left-bottom
        [x0, y0, z1],  # 4: bottom-left-top
        [x1, y0, z1],  # 5: bottom-right-top
        [x1, y1, z1],  # 6: top-right-top
        [x0, y1, z1],  # 7: top-left-top
    ]

    triangles = [
        # Bottom face
        (0, 2, 1),
        (0, 3, 2),
        # Top face
        (4, 5, 6),
        (4, 6, 7),
        # Front face
        (0, 1, 5),
        (0, 5, 4),
        # Back face
        (3, 7, 6),
        (3, 6, 2),
        # Left face
        (0, 4, 7),
        (0, 7, 3),
        # Right face
        (1, 2, 6),
        (1, 6, 5),
    ]

    return vertices, triangles


def generate_box_geometry_no_bottom(
    x0: float, y0: float, z0: float, x1: float, y1: float, z1: float
) -> tuple[list[list[float]], list[tuple[int, int, int]]]:
    """Generate vertices and triangles for a 3D box without bottom face.

    Args:
        x0, y0, z0: Minimum coordinates
        x1, y1, z1: Maximum coordinates

    Returns:
        Tuple of (vertices, triangles) where:
        - vertices is a list of [x, y, z] coordinates
        - triangles is a list of (v0, v1, v2) vertex indices
    """
    vertices = [
        [x0, y0, z0],  # 0: bottom-left-bottom
        [x1, y0, z0],  # 1: bottom-right-bottom
        [x1, y1, z0],  # 2: top-right-bottom
        [x0, y1, z0],  # 3: top-left-bottom
        [x0, y0, z1],  # 4: bottom-left-top
        [x1, y0, z1],  # 5: bottom-right-top
        [x1, y1, z1],  # 6: top-right-top
        [x0, y1, z1],  # 7: top-left-top
    ]

    triangles = [
        # NO BOTTOM FACE - this sits on the base plate
        # Top face
        (4, 5, 6),
        (4, 6, 7),
        # Front face
        (0, 1, 5),
        (0, 5, 4),
        # Back face
        (3, 7, 6),
        (3, 6, 2),
        # Left face
        (0, 4, 7),
        (0, 7, 3),
        # Right face
        (1, 2, 6),
        (1, 6, 5),
    ]

    return vertices, triangles


def generate_box_geometry_no_top(
    x0: float, y0: float, z0: float, x1: float, y1: float, z1: float
) -> tuple[list[list[float]], list[tuple[int, int, int]]]:
    """Generate vertices and triangles for a 3D box without top face.

    Args:
        x0, y0, z0: Minimum coordinates
        x1, y1, z1: Maximum coordinates

    Returns:
        Tuple of (vertices, triangles) where:
        - vertices is a list of [x, y, z] coordinates
        - triangles is a list of (v0, v1, v2) vertex indices
    """
    vertices = [
        [x0, y0, z0],  # 0: bottom-left-bottom
        [x1, y0, z0],  # 1: bottom-right-bottom
        [x1, y1, z0],  # 2: top-right-bottom
        [x0, y1, z0],  # 3: top-left-bottom
        [x0, y0, z1],  # 4: bottom-left-top
        [x1, y0, z1],  # 5: bottom-right-top
        [x1, y1, z1],  # 6: top-right-top
        [x0, y1, z1],  # 7: top-left-top
    ]

    triangles = [
        # Bottom face
        (0, 2, 1),
        (0, 3, 2),
        # NO TOP FACE - individual pixels provide the top surface
        # Front face
        (0, 1, 5),
        (0, 5, 4),
        # Back face
        (3, 7, 6),
        (3, 6, 2),
        # Left face
        (0, 4, 7),
        (0, 7, 3),
        # Right face
        (1, 2, 6),
        (1, 6, 5),
    ]

    return vertices, triangles


def generate_qr_geometry(
    height_map: np.ndarray,
    pixel_size: float,
    base_height: float,
    include_base: bool = True,
    include_walls: bool = True,
) -> tuple[list[list[float]], list[tuple[int, int, int]], dict[str, list[int]]]:
    """Generate complete QR code geometry from height map.

    Args:
        height_map: 2D array of heights
        pixel_size: Size of each pixel in mm
        base_height: Height of the base layer
        include_base: Whether to include the base plate
        include_walls: Whether to include internal walls at height transitions

    Returns:
        Tuple of (vertices, triangles, component_info) where:
        - vertices is a list of [x, y, z] coordinates
        - triangles is a list of (v0, v1, v2) vertex indices
        - component_info contains lists of triangle indices for different components
    """
    height, width = height_map.shape
    vertices = []
    triangles = []
    component_info = {"base_triangles": [], "qr_triangles": [], "wall_triangles": [], "top_surface_triangles": []}

    # Generate top surface for ALL pixels (matching main branch approach)
    for y in range(height):
        for x in range(width):
            # Get the four corners of this pixel
            x0, y0 = x * pixel_size, y * pixel_size
            x1, y1 = (x + 1) * pixel_size, (y + 1) * pixel_size

            # Height for this pixel
            h = height_map[y, x]

            # Add vertices for this pixel's top surface
            v_start = len(vertices)
            vertices.extend([[x0, y0, h], [x1, y0, h], [x1, y1, h], [x0, y1, h]])

            # Add triangles for top surface
            tri_start = len(triangles)
            # First triangle
            triangles.append((v_start, v_start + 1, v_start + 3))
            # Second triangle
            triangles.append((v_start + 1, v_start + 2, v_start + 3))

            # Track which component these triangles belong to
            if abs(h - base_height) < 0.001:
                # This is at base height
                component_info["base_triangles"].extend(range(tri_start, len(triangles)))
            else:
                # This is a raised QR module
                component_info["qr_triangles"].extend(range(tri_start, len(triangles)))

    if include_walls:
        # Generate internal walls at height transitions
        for y in range(height):
            for x in range(width):
                x0, y0 = x * pixel_size, y * pixel_size
                x1, y1 = (x + 1) * pixel_size, (y + 1) * pixel_size
                curr_height = height_map[y, x]

                # Check right neighbor
                if x < width - 1:
                    right_height = height_map[y, x + 1]
                    if curr_height != right_height:
                        min_h = min(curr_height, right_height)
                        max_h = max(curr_height, right_height)

                        # Add wall vertices
                        v_start = len(vertices)
                        vertices.extend([[x1, y0, min_h], [x1, y1, min_h], [x1, y1, max_h], [x1, y0, max_h]])

                        # Add wall triangles
                        tri_start = len(triangles)
                        if curr_height > right_height:
                            # Normal points left
                            triangles.extend(
                                [(v_start, v_start + 1, v_start + 3), (v_start + 1, v_start + 2, v_start + 3)]
                            )
                        else:
                            # Normal points right
                            triangles.extend(
                                [(v_start, v_start + 3, v_start + 1), (v_start + 3, v_start + 2, v_start + 1)]
                            )
                        component_info["wall_triangles"].extend(range(tri_start, len(triangles)))

                # Check bottom neighbor
                if y < height - 1:
                    bottom_height = height_map[y + 1, x]
                    if curr_height != bottom_height:
                        min_h = min(curr_height, bottom_height)
                        max_h = max(curr_height, bottom_height)

                        # Add wall vertices
                        v_start = len(vertices)
                        vertices.extend([[x0, y1, min_h], [x1, y1, min_h], [x1, y1, max_h], [x0, y1, max_h]])

                        # Add wall triangles
                        tri_start = len(triangles)
                        if curr_height > bottom_height:
                            # Normal points up
                            triangles.extend(
                                [(v_start, v_start + 1, v_start + 3), (v_start + 1, v_start + 2, v_start + 3)]
                            )
                        else:
                            # Normal points down
                            triangles.extend(
                                [(v_start, v_start + 3, v_start + 1), (v_start + 3, v_start + 2, v_start + 1)]
                            )
                        component_info["wall_triangles"].extend(range(tri_start, len(triangles)))

    # Generate outer walls
    total_width = width * pixel_size
    total_depth = height * pixel_size

    # Front wall (y=0)
    for x in range(width):
        x0, x1 = x * pixel_size, (x + 1) * pixel_size
        h = height_map[0, x]

        v_start = len(vertices)
        vertices.extend([[x0, 0, 0], [x1, 0, 0], [x1, 0, h], [x0, 0, h]])

        tri_start = len(triangles)
        triangles.extend([(v_start, v_start + 1, v_start + 3), (v_start + 1, v_start + 2, v_start + 3)])
        component_info["wall_triangles"].extend(range(tri_start, len(triangles)))

    # Back wall (y=max)
    for x in range(width):
        x0, x1 = x * pixel_size, (x + 1) * pixel_size
        h = height_map[height - 1, x]

        v_start = len(vertices)
        vertices.extend([[x0, total_depth, h], [x1, total_depth, h], [x1, total_depth, 0], [x0, total_depth, 0]])

        tri_start = len(triangles)
        triangles.extend([(v_start, v_start + 1, v_start + 3), (v_start + 1, v_start + 2, v_start + 3)])
        component_info["wall_triangles"].extend(range(tri_start, len(triangles)))

    # Left wall (x=0)
    for y in range(height):
        y0, y1 = y * pixel_size, (y + 1) * pixel_size
        h = height_map[y, 0]

        v_start = len(vertices)
        vertices.extend([[0, y0, h], [0, y1, h], [0, y1, 0], [0, y0, 0]])

        tri_start = len(triangles)
        triangles.extend([(v_start, v_start + 1, v_start + 3), (v_start + 1, v_start + 2, v_start + 3)])
        component_info["wall_triangles"].extend(range(tri_start, len(triangles)))

    # Right wall (x=max)
    for y in range(height):
        y0, y1 = y * pixel_size, (y + 1) * pixel_size
        h = height_map[y, width - 1]

        v_start = len(vertices)
        vertices.extend([[total_width, y0, 0], [total_width, y1, 0], [total_width, y1, h], [total_width, y0, h]])

        tri_start = len(triangles)
        triangles.extend([(v_start, v_start + 1, v_start + 3), (v_start + 1, v_start + 2, v_start + 3)])
        component_info["wall_triangles"].extend(range(tri_start, len(triangles)))

    if include_base:
        # Generate bottom face - solid base covering entire area
        v_start = len(vertices)
        vertices.extend([[0, 0, 0], [total_width, 0, 0], [total_width, total_depth, 0], [0, total_depth, 0]])

        # Add bottom face triangles
        tri_start = len(triangles)
        triangles.append((v_start, v_start + 1, v_start + 3))
        triangles.append((v_start + 1, v_start + 2, v_start + 3))
        component_info["base_triangles"].extend(range(tri_start, len(triangles)))

    return vertices, triangles, component_info


def generate_keychain_mount_geometry(
    base_width: float,
    base_depth: float,
    base_height: float,
    hole_diameter: float = 4.0,
    tab_width: float = 15.0,
    tab_height: float = 10.0,
    tab_thickness: float = 2.0,
) -> tuple[list[list[float]], list[tuple[int, int, int]]]:
    """Generate vertex and triangle data for a keychain mounting tab.

    Args:
        base_width: Width of the QR code base in mm
        base_depth: Depth of the QR code base in mm
        base_height: Height of the base in mm
        hole_diameter: Diameter of the keychain hole in mm
        tab_width: Width of the mounting tab in mm
        tab_height: Height of the mounting tab in mm
        tab_thickness: Thickness of the tab in mm

    Returns:
        Tuple of (vertices, triangles) where:
        - vertices is a list of [x, y, z] coordinates
        - triangles is a list of (v0, v1, v2) vertex indices
    """
    # The tab is a "D" shape: straight sides rising from the QR base edge, closed
    # by a semicircle concentric with the hole so the ring around it is even.
    tab_y_start = base_depth  # Tab starts at the base edge
    hole_radius = hole_diameter / 2
    arc_radius = max(tab_width / 2, hole_radius + 1.0)
    hole_center_x = base_width / 2
    # Place the arc top at the tab height, but keep at least 1mm between hole and base edge
    hole_center_y = max(tab_y_start + tab_height - arc_radius, tab_y_start + hole_radius + 1.0)

    # Sample angles around the hole center, offset by half a step so the sampled
    # angles never coincide with the two square corners added below
    angles = [2 * np.pi * (i + 0.5) / KEYCHAIN_SEGMENTS for i in range(KEYCHAIN_SEGMENTS)]
    corner_dy = tab_y_start - hole_center_y
    angles.append(float(np.arctan2(corner_dy, -arc_radius)) % (2 * np.pi))
    angles.append(float(np.arctan2(corner_dy, arc_radius)) % (2 * np.pi))
    angles.sort()

    # Outer outline point for each angle: arc on the upper half, rectangle below
    outer_points = []
    for angle in angles:
        cos_a, sin_a = float(np.cos(angle)), float(np.sin(angle))
        if sin_a >= 0:
            dist = arc_radius
        else:
            dist = corner_dy / sin_a  # Distance to the base edge
            if abs(cos_a) > 1e-9:
                dist = min(dist, arc_radius / abs(cos_a))  # Distance to a straight side
        outer_points.append((hole_center_x + dist * cos_a, hole_center_y + dist * sin_a))

    hole_points = [
        (hole_center_x + hole_radius * float(np.cos(a)), hole_center_y + hole_radius * float(np.sin(a))) for a in angles
    ]

    num_points = len(angles)
    vertices: list[list[float]] = []
    triangles: list[tuple[int, int, int]] = []

    # Vertex layout: outer top, outer bottom, hole top, hole bottom
    outer_top = 0
    outer_bottom = num_points
    hole_top = 2 * num_points
    hole_bottom = 3 * num_points
    for x, y in outer_points:
        vertices.append([x, y, tab_thickness])
    for x, y in outer_points:
        vertices.append([x, y, 0])
    for x, y in hole_points:
        vertices.append([x, y, tab_thickness])
    for x, y in hole_points:
        vertices.append([x, y, 0])

    for i in range(num_points):
        j = (i + 1) % num_points

        # Top face ring (counter-clockwise viewed from above, normal +Z)
        triangles.append((outer_top + i, outer_top + j, hole_top + j))
        triangles.append((outer_top + i, hole_top + j, hole_top + i))

        # Bottom face ring (reversed winding, normal -Z)
        triangles.append((outer_bottom + i, hole_bottom + j, outer_bottom + j))
        triangles.append((outer_bottom + i, hole_bottom + i, hole_bottom + j))

        # Outer wall, normal pointing away from the tab
        triangles.append((outer_bottom + i, outer_bottom + j, outer_top + j))
        triangles.append((outer_bottom + i, outer_top + j, outer_top + i))

        # Hole wall, normal pointing into the hole
        triangles.append((hole_top + i, hole_top + j, hole_bottom + i))
        triangles.append((hole_top + j, hole_bottom + j, hole_bottom + i))

    return vertices, triangles


def add_keychain_mount(
    stl_mesh: Any,
    face_idx: int,
    base_width: float,
    base_depth: float,
    base_height: float,
    hole_diameter: float = 4.0,
    tab_width: float = 15.0,
    tab_height: float = 10.0,
) -> int:
    """Add a keychain mounting tab with hole to an STL mesh.

    Args:
        stl_mesh: The STL mesh object to add mount to
        face_idx: Current face index to start adding faces
        base_width: Width of the QR code base in mm
        base_depth: Depth of the QR code base in mm
        base_height: Height of the base in mm
        hole_diameter: Diameter of the keychain hole in mm
        tab_width: Width of the mounting tab in mm
        tab_height: Height of the mounting tab in mm

    Returns:
        Updated face index after adding mount faces
    """
    # Generate the geometry with a thinner tab (2mm thickness)
    vertices, triangles = generate_keychain_mount_geometry(
        base_width, base_depth, base_height, hole_diameter, tab_width, tab_height, tab_thickness=2.0
    )

    # Add triangles to STL mesh
    for v0, v1, v2 in triangles:
        stl_mesh.vectors[face_idx] = np.array([vertices[v0], vertices[v1], vertices[v2]])
        face_idx += 1

    return face_idx


def add_keychain_mount_3mf(
    mesh_object: Any,
    base_width: float,
    base_depth: float,
    base_height: float,
    hole_diameter: float,
    material_group: Any | None,
    material_id: int | None,
    triangle_properties: list | None,
    wrapper: Any,
    tab_width: float = 15.0,
    tab_height: float = 10.0,
) -> None:
    """Add a keychain mounting tab with hole to a 3MF mesh object.

    Args:
        mesh_object: The 3MF mesh object to add mount to
        base_width: Width of the QR code base in mm
        base_depth: Depth of the QR code base in mm
        base_height: Height of the base in mm
        hole_diameter: Diameter of the keychain hole in mm
        material_group: Material group for coloring (None if separate components)
        material_id: Material ID for the mount
        triangle_properties: List to append triangle properties to
        wrapper: lib3mf wrapper instance
        tab_width: Width of the mounting tab in mm
        tab_height: Height of the mounting tab in mm
    """
    # Generate the shared geometry with a thinner tab (2mm thickness)
    vertices, triangles = generate_keychain_mount_geometry(
        base_width, base_depth, base_height, hole_diameter, tab_width, tab_height, tab_thickness=2.0
    )

    # Add vertices to 3MF mesh and store their indices
    vertex_indices = []
    for x, y, z in vertices:
        position = lib3mf.Position()
        position.Coordinates[0] = float(x)
        position.Coordinates[1] = float(y)
        position.Coordinates[2] = float(z)
        vertex_indices.append(mesh_object.AddVertex(position))

    # Add triangles to 3MF mesh
    for v0, v1, v2 in triangles:
        triangle = lib3mf.Triangle()
        triangle.Indices[0] = vertex_indices[v0]
        triangle.Indices[1] = vertex_indices[v1]
        triangle.Indices[2] = vertex_indices[v2]
        mesh_object.AddTriangle(triangle)

        if material_group is not None and triangle_properties is not None:
            prop = lib3mf.TriangleProperties()
            prop.ResourceID = material_group.GetResourceID()
            prop.PropertyIDs[0] = material_id
            prop.PropertyIDs[1] = material_id
            prop.PropertyIDs[2] = material_id
            triangle_properties.append(prop)


def convert_qr_to_stl(
    qr_image: Image.Image,
    output_path: Path | str,
    base_size_mm: tuple[float, float] = (50.0, 50.0),
    base_height_mm: float = 2.0,
    qr_height_mm: float = 2.0,
    invert: bool = False,
    multi_layer: bool = False,
    layer_heights: list[float] | None = None,
    has_frame: bool = False,
    mount_type: str | None = None,
    hole_diameter: float = 4.0,
    debug: bool = False,
    back_text: str | None = None,
    back_text_size_mm: float = 4.0,
    back_text_depth_mm: float = 0.6,
) -> Path:
    """Convert a QR code image to an STL file.

    This function uses an optimized surface-based approach for STL generation:
    - Creates a complete top surface mesh (2 triangles per pixel)
    - Adds internal walls between pixels of different heights
    - Includes outer walls and a solid bottom face
    - Results in efficient, watertight meshes suitable for 3D printing

    The STL format contains only geometry data (no color information) and is
    optimized for single-material 3D printing. The internal walls ensure proper
    slicing behavior in 3D printing software.

    Args:
        qr_image: PIL Image of the QR code
        output_path: Path to save the STL file
        base_size_mm: Size of the base in mm (width, height)
        base_height_mm: Height of the base layer in mm
        qr_height_mm: Additional height for QR code modules in mm
        invert: If True, white areas are raised instead of black
        multi_layer: If True, create multiple distinct layer heights
        layer_heights: List of layer heights [base, qr, frame] in mm
        has_frame: If True, indicates the image has a frame to be rendered at a different height
        mount_type: Type of mounting feature ('keychain' or 'holes')
        hole_diameter: Diameter of mounting holes in mm
        debug: If True, enable mesh validation and save debug visualization
        back_text: Optional text recessed (mirrored) into the bottom face
        back_text_size_mm: Height of capital letters in the back text in mm
        back_text_depth_mm: How deep the back text is recessed in mm

    Returns:
        Path to the saved STL file

    Raises:
        ValueError: If the back text is too deep for the base or does not fit
    """
    # Prepare output path with .stl extension
    output_path = prepare_output_path(output_path, ".stl")

    # Convert image to height map
    if multi_layer and layer_heights:
        height_map = image_to_multilayer_3d_array(
            qr_image,
            layer_heights=layer_heights,
            invert=invert,
            has_frame=has_frame,
        )
        # Update base_height_mm and qr_height_mm for logging
        base_height_mm = layer_heights[0]
        if len(layer_heights) >= 2:
            qr_height_mm = layer_heights[1] - layer_heights[0]
    else:
        height_map = image_to_3d_array(
            qr_image,
            base_height=base_height_mm,
            qr_height=qr_height_mm,
            invert=invert,
        )

    # Flip the height map vertically to correct the orientation
    # This ensures labels at the top of the image appear at the top of the STL
    height_map = np.flipud(height_map)

    # Calculate pixel size to achieve desired base size
    img_height, img_width = height_map.shape
    pixel_size_x = base_size_mm[0] / img_width
    pixel_size_y = base_size_mm[1] / img_height

    # Use uniform pixel size (average of x and y)
    pixel_size = (pixel_size_x + pixel_size_y) / 2

    logger.debug(f"Image size: {img_width}x{img_height}, pixel size: {pixel_size:.3f}mm")

    # Prepare debug path if debug mode is enabled
    debug_path = None
    if debug:
        debug_path = output_path.with_suffix(".debug.png")

    # Recess back text into the bottom face, keeping at least 0.4mm of material above it
    bottom_map = None
    if back_text:
        thinnest = float(np.min(height_map))
        if back_text_depth_mm > thinnest - 0.4:
            raise ValueError(
                f"Back text depth {back_text_depth_mm}mm is too deep for a {thinnest}mm base. "
                f"Use a depth of at most {thinnest - 0.4:.1f}mm or a thicker base."
            )
        text_mask = render_back_text_mask(back_text, height_map.shape, pixel_size, back_text_size_mm)
        bottom_map = np.where(text_mask, back_text_depth_mm, 0.0)

    # Create STL mesh
    stl_mesh = create_stl_from_heightmap(
        height_map,
        pixel_size=pixel_size,
        mount_type=mount_type,
        hole_diameter=hole_diameter,
        validate_and_repair=True,
        debug_path=debug_path,
        bottom_map=bottom_map,
    )

    # Save STL file
    stl_mesh.save(str(output_path))

    if multi_layer and layer_heights:
        total_height = max(layer_heights)
    else:
        total_height = base_height_mm + qr_height_mm

    logger.info(f"Saved STL file to: {output_path}")
    logger.info(f"Model dimensions: {base_size_mm[0]:.1f} x {base_size_mm[1]:.1f} x {total_height:.1f} mm")

    if multi_layer and layer_heights:
        logger.info(f"Multi-layer heights: {', '.join(f'{h:.1f}mm' for h in layer_heights)}")

    if mount_type:
        logger.info(f"Added {mount_type} mount with {hole_diameter:.1f}mm hole diameter")

    if back_text:
        logger.info(f"Recessed back text {back_text_depth_mm:.1f}mm into the bottom face")

    return output_path


def convert_qr_to_3mf(
    qr_image: Image.Image,
    output_path: Path | str,
    base_size_mm: tuple[float, float] = (50.0, 50.0),
    base_height_mm: float = 2.0,
    qr_height_mm: float = 2.0,
    invert: bool = False,
    base_color: str = "white",
    qr_color: str = "black",
    separate_components: bool = False,
    mount_type: str | None = None,
    hole_diameter: float = 4.0,
) -> Path:
    """Convert a QR code image to a 3MF file with color support using lib3mf.

    This function uses a complete 3D object approach for 3MF generation:
    - Each raised QR module is created as a complete 3D box (all 6 faces)
    - The base plate is a complete 3D box with all faces
    - Each component can have its own color/material assignment
    - Supports both single mesh with materials and separate mesh objects

    The 3MF format is designed for multi-material/multi-color 3D printing and
    embeds color and material information directly in the file. This approach
    creates larger files than STL but enables advanced printing features like:
    - Multi-color printing on compatible printers
    - Material assignment per component
    - Better integration with modern slicing software

    Note: Unlike STL generation, 3MF does not need internal walls as the
    complete box geometry provides all necessary structure for slicing.

    Args:
        qr_image: PIL Image of the QR code
        output_path: Path to save the 3MF file
        base_size_mm: Size of the base in mm (width, height)
        base_height_mm: Height of the base layer in mm
        qr_height_mm: Additional height for QR code modules in mm
        invert: If True, white areas are raised instead of black
        base_color: Color name or hex code for the base/background
        qr_color: Color name or hex code for the QR modules
        separate_components: If True, create separate objects for each color
        mount_type: Type of mounting feature ('keychain' or 'holes')
        hole_diameter: Diameter of mounting holes in mm

    Returns:
        Path to the saved 3MF file
    """
    if not HAS_LIB3MF:
        raise ImportError("lib3mf is required for colored 3MF export. Install with: pip install lib3mf")

    # Prepare output path with .3mf extension
    output_path = prepare_output_path(output_path, ".3mf")

    # Parse colors to RGB
    base_rgb = parse_color(base_color, "white")
    qr_rgb = parse_color(qr_color, "black")

    # Convert image to height map
    height_map = image_to_3d_array(
        qr_image,
        base_height=base_height_mm,
        qr_height=qr_height_mm,
        invert=invert,
    )

    # Flip the height map vertically to correct the orientation
    height_map = np.flipud(height_map)

    # Calculate pixel size to achieve desired base size
    img_height, img_width = height_map.shape
    pixel_size_x = base_size_mm[0] / img_width
    pixel_size_y = base_size_mm[1] / img_height
    pixel_size = (pixel_size_x + pixel_size_y) / 2

    logger.debug(f"Image size: {img_width}x{img_height}, pixel size: {pixel_size:.3f}mm")

    # Initialize lib3mf
    wrapper = get_wrapper()
    model = wrapper.CreateModel()  # type: ignore[attr-defined]

    if separate_components:
        # Create separate mesh objects for each color
        base_mesh = model.AddMeshObject()  # type: ignore[attr-defined]
        base_mesh.SetName("Base")

        qr_mesh = model.AddMeshObject()  # type: ignore[attr-defined]
        qr_mesh.SetName("QR Modules")

        # Create color group for separate components
        color_group = model.AddColorGroup()  # type: ignore[attr-defined]

        # Add colors
        base_color_id = color_group.AddColor(color_to_3mf_format(base_rgb, wrapper))
        qr_color_id = color_group.AddColor(color_to_3mf_format(qr_rgb, wrapper))

        # Set object colors
        base_mesh.SetObjectLevelProperty(color_group.GetResourceID(), base_color_id)
        qr_mesh.SetObjectLevelProperty(color_group.GetResourceID(), qr_color_id)
    else:
        # Single object with material group
        material_group = model.AddBaseMaterialGroup()  # type: ignore[attr-defined]

        # Add materials with colors
        base_material_id = material_group.AddMaterial(
            f"{base_color} base",
            color_to_3mf_format(base_rgb, wrapper),
        )
        qr_material_id = material_group.AddMaterial(
            f"{qr_color} QR",
            color_to_3mf_format(qr_rgb, wrapper),
        )

        # Create single mesh object
        mesh_object = model.AddMeshObject()  # type: ignore[attr-defined]
        mesh_object.SetName("QR Code")
        triangle_properties = []

    # Build QR modules
    for y in range(img_height):
        for x in range(img_width):
            x0, y0 = x * pixel_size, y * pixel_size
            x1, y1 = (x + 1) * pixel_size, (y + 1) * pixel_size
            z0 = base_height_mm  # Start from base height
            z1 = height_map[y, x]

            # Skip if this pixel is at base height (no need to create a box)
            if abs(z1 - base_height_mm) < 0.001:
                continue

            # Choose which mesh to add to
            current_mesh = qr_mesh if separate_components else mesh_object

            # Add 8 vertices for the box
            vertices_indices = []
            for vx, vy, vz in [
                (x0, y0, z0),
                (x1, y0, z0),
                (x1, y1, z0),
                (x0, y1, z0),
                (x0, y0, z1),
                (x1, y0, z1),
                (x1, y1, z1),
                (x0, y1, z1),
            ]:
                position = lib3mf.Position()
                position.Coordinates[0] = float(vx)
                position.Coordinates[1] = float(vy)
                position.Coordinates[2] = float(vz)
                vertices_indices.append(current_mesh.AddVertex(position))

            # Create triangles for the box (12 triangles, 2 per face)
            triangle_indices = [
                # Bottom face
                (0, 2, 1),
                (0, 3, 2),
                # Top face
                (4, 5, 6),
                (4, 6, 7),
                # Front face
                (0, 1, 5),
                (0, 5, 4),
                # Back face
                (3, 7, 6),
                (3, 6, 2),
                # Left face
                (0, 4, 7),
                (0, 7, 3),
                # Right face
                (1, 2, 6),
                (1, 6, 5),
            ]

            # Add triangles and set material
            for v0, v1, v2 in triangle_indices:
                triangle = lib3mf.Triangle()
                triangle.Indices[0] = vertices_indices[v0]
                triangle.Indices[1] = vertices_indices[v1]
                triangle.Indices[2] = vertices_indices[v2]
                current_mesh.AddTriangle(triangle)

                if not separate_components:
                    # Create triangle properties with QR material
                    prop = lib3mf.TriangleProperties()
                    prop.ResourceID = material_group.GetResourceID()
                    prop.PropertyIDs[0] = qr_material_id
                    prop.PropertyIDs[1] = qr_material_id
                    prop.PropertyIDs[2] = qr_material_id
                    triangle_properties.append(prop)

    # Add complete base plate as a box
    base_width = img_width * pixel_size
    base_depth = img_height * pixel_size

    # Choose which mesh to add base to
    base_target_mesh = base_mesh if separate_components else mesh_object

    # Create 8 vertices for the base box
    base_vertices = []
    for vx, vy, vz in [
        (0, 0, 0),  # 0: bottom-left-bottom
        (base_width, 0, 0),  # 1: bottom-right-bottom
        (base_width, base_depth, 0),  # 2: top-right-bottom
        (0, base_depth, 0),  # 3: top-left-bottom
        (0, 0, base_height_mm),  # 4: bottom-left-top
        (base_width, 0, base_height_mm),  # 5: bottom-right-top
        (base_width, base_depth, base_height_mm),  # 6: top-right-top
        (0, base_depth, base_height_mm),  # 7: top-left-top
    ]:
        position = lib3mf.Position()
        position.Coordinates[0] = float(vx)
        position.Coordinates[1] = float(vy)
        position.Coordinates[2] = float(vz)
        base_vertices.append(base_target_mesh.AddVertex(position))

    # Create triangles for all 6 faces of the base box
    base_triangle_indices = [
        # Bottom face
        (0, 2, 1),
        (0, 3, 2),
        # Top face
        (4, 5, 6),
        (4, 6, 7),
        # Front face
        (0, 1, 5),
        (0, 5, 4),
        # Back face
        (3, 7, 6),
        (3, 6, 2),
        # Left face
        (0, 4, 7),
        (0, 7, 3),
        # Right face
        (1, 2, 6),
        (1, 6, 5),
    ]

    for v0, v1, v2 in base_triangle_indices:
        triangle = lib3mf.Triangle()
        triangle.Indices[0] = base_vertices[v0]
        triangle.Indices[1] = base_vertices[v1]
        triangle.Indices[2] = base_vertices[v2]
        base_target_mesh.AddTriangle(triangle)

        if not separate_components:
            # Create triangle properties with base material
            prop = lib3mf.TriangleProperties()
            prop.ResourceID = material_group.GetResourceID()
            prop.PropertyIDs[0] = base_material_id
            prop.PropertyIDs[1] = base_material_id
            prop.PropertyIDs[2] = base_material_id
            triangle_properties.append(prop)

    # Add mounting features if requested
    if mount_type == "keychain":
        # Get dimensions
        img_height, img_width = height_map.shape
        base_width = img_width * pixel_size
        base_depth = img_height * pixel_size

        # Choose which mesh to add mount to
        mount_target_mesh = base_mesh if separate_components else mesh_object

        # Add keychain mount using similar logic as STL version
        add_keychain_mount_3mf(
            mount_target_mesh,
            base_width,
            base_depth,
            base_height_mm,  # Use base height, not max height
            hole_diameter,
            material_group if not separate_components else None,
            base_material_id if not separate_components else None,
            triangle_properties if not separate_components else None,
            wrapper,
        )

    # Apply properties and add build items
    if separate_components:
        # Add both meshes as build items
        model.AddBuildItem(base_mesh, wrapper.GetIdentityTransform())  # type: ignore[attr-defined]
        model.AddBuildItem(qr_mesh, wrapper.GetIdentityTransform())  # type: ignore[attr-defined]
    else:
        # Apply all triangle properties to single mesh
        mesh_object.SetAllTriangleProperties(triangle_properties)
        # Add single build item
        model.AddBuildItem(mesh_object, wrapper.GetIdentityTransform())  # type: ignore[attr-defined]

    # Write to file
    writer = model.QueryWriter("3mf")  # type: ignore[attr-defined]
    writer.WriteToFile(str(output_path))

    total_height = base_height_mm + qr_height_mm
    logger.info(f"Saved 3MF file to: {output_path}")
    logger.info(f"Model dimensions: {base_size_mm[0]:.1f} x {base_size_mm[1]:.1f} x {total_height:.1f} mm")
    logger.info(f"Colors: base={base_color}, QR={qr_color}")

    if mount_type:
        logger.info(f"Added {mount_type} mount with {hole_diameter:.1f}mm hole diameter")

    return output_path
