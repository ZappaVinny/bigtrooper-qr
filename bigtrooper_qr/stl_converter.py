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

import io
import logging
import math
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

from .errors import QRModelError
from .utils import color_to_3mf_format, ensure_grayscale, load_font_with_fallbacks, parse_color

logger = logging.getLogger(__name__)

# Keychain tab outline resolution: evenly spaced samples plus the two square corners
KEYCHAIN_SEGMENTS = 32

# Arch mount resolution: segments along the half-ellipse from one leg to the other
ARCH_SEGMENTS = 48

# 3D-printed mounts are a fixed thickness, whatever the base thickness
MOUNT_THICKNESS_MM = 2.0

# Minimum clearance between back text and the edge of the base
BACK_TEXT_MARGIN_MM = 1.5

# Outline points per rounded base corner in 3MF output
ROUNDED_CORNER_SEGMENTS = 16


def rounded_rect_mask(shape: tuple[int, int], pixel_size: float, radius: float, round_top: bool = True) -> np.ndarray:
    """Mark the pixels whose centers fall inside a rectangle with rounded corners.

    Args:
        shape: (rows, columns) of the height map
        pixel_size: Size of each pixel in mm
        radius: Corner radius in mm
        round_top: If False, the two corners at the top (high y, where mounts attach) stay square

    Returns:
        Boolean array of the given shape, True for pixels inside the outline
    """
    rows, cols = shape
    xs = (np.arange(cols) + 0.5) * pixel_size
    ys = (np.arange(rows) + 0.5) * pixel_size
    # Distance from each pixel center to the nearest point of the inner rectangle
    # (the outline shrunk by the radius); inside the outline means within the radius
    dx = xs - np.clip(xs, radius, cols * pixel_size - radius)
    dy = ys - np.clip(ys, radius, rows * pixel_size - (radius if round_top else 0.0))
    return dx[np.newaxis, :] ** 2 + dy[:, np.newaxis] ** 2 <= radius**2


def rounded_rect_outline(
    width: float, depth: float, radius: float, round_top: bool = True
) -> list[tuple[float, float]]:
    """Outline of a rectangle with rounded corners, counter-clockwise from the bottom-right corner.

    Args:
        width: Width of the rectangle in mm (x)
        depth: Depth of the rectangle in mm (y)
        radius: Corner radius in mm; 0 gives the four plain corners
        round_top: If False, the two corners at the top (high y, where mounts attach) stay square

    Returns:
        List of (x, y) outline points
    """
    centers = [(width - radius, radius), (width - radius, depth - radius), (radius, depth - radius), (radius, radius)]
    points = []
    for corner, (cx, cy) in enumerate(centers):
        is_top = corner in (1, 2)
        if radius <= 0 or (is_top and not round_top):
            points.append(((width if corner < 2 else 0.0), (depth if is_top else 0.0)))
            continue
        start = -np.pi / 2 + corner * np.pi / 2
        for i in range(ROUNDED_CORNER_SEGMENTS + 1):
            angle = start + (np.pi / 2) * i / ROUNDED_CORNER_SEGMENTS
            points.append((cx + radius * float(np.cos(angle)), cy + radius * float(np.sin(angle))))
    return points


def render_back_text_mask(
    text: str,
    grid_shape: tuple[int, int],
    pixel_size: float,
    text_size_mm: float = 4.0,
    warnings: list[str] | None = None,
) -> np.ndarray:
    """Render text as a mask for recessing into the bottom face of the model.

    The text is centered on the base and mirrored so it reads correctly when the
    model is flipped over. If it does not fit at the requested size it is shrunk.

    Args:
        text: Text to render. Lines are separated by newlines or a literal "\\n".
        grid_shape: (rows, cols) of the height map the mask must match
        pixel_size: Size of each height map pixel in mm
        text_size_mm: Target height of capital letters in mm
        warnings: Optional list that receives a message if the text had to shrink

    Returns:
        Boolean array with the height map's shape, True where text is recessed

    Raises:
        QRModelError: If the text is empty or cannot fit on the base
    """
    lines = [line.strip() for line in text.replace("\\n", "\n").split("\n") if line.strip()]
    if not lines:
        raise QRModelError("Back text is empty")
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
            raise QRModelError(f"Back text '{joined}' does not fit on the base. Use shorter text or a larger base.")
        font_size = max(4, int(font_size * 0.9))

    if font_size < requested_size and warnings is not None:
        warnings.append(
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
    mount_geometry: tuple[list[list[float]], list[tuple[int, int, int]]] | None = None,
    bottom_map: np.ndarray | None = None,
    mask: np.ndarray | None = None,
) -> Any:
    """Create an STL mesh from a height map.

    Args:
        height_map: 2D array of heights
        pixel_size: Size of each pixel in mm
        mount_geometry: Optional (vertices, triangles) of a mount to add, from generate_mount_geometry
        bottom_map: Optional 2D array of bottom face heights, same shape as height_map.
            Values above 0 are recessed into the bottom (used for back text).
        mask: Optional boolean array, same shape as height_map. Only True pixels are solid,
            and outer walls follow its edge (used for rounded corners). Defaults to all True.

    Returns:
        STL Mesh object with optional mounting features
    """
    height, width = height_map.shape

    flat_bottom = bottom_map is None and mask is None
    if mask is None:
        mask = np.ones(height_map.shape, dtype=bool)
    bottom_map = np.zeros_like(height_map) if bottom_map is None else np.where(mask, bottom_map, 0.0)

    # Neighbors that are both solid, and solid pixel sides that face empty space
    # (the grid border counts as empty)
    pair_x = mask[:, 1:] & mask[:, :-1]
    pair_y = mask[1:, :] & mask[:-1, :]
    padded = np.pad(mask, 1, constant_values=False)
    open_front = mask & ~padded[:-2, 1:-1]  # y - 1
    open_back = mask & ~padded[2:, 1:-1]  # y + 1
    open_left = mask & ~padded[1:-1, :-2]  # x - 1
    open_right = mask & ~padded[1:-1, 2:]  # x + 1

    # Top surface: 2 triangles per solid pixel
    num_faces = 2 * int(np.count_nonzero(mask))

    if flat_bottom:
        # Bottom surface: 2 triangles for entire base
        num_faces += 2
    else:
        # Bottom surface: 2 triangles per solid pixel plus walls at each recess edge
        num_faces += 2 * int(np.count_nonzero(mask))
        num_faces += 2 * int(np.count_nonzero(pair_x & (bottom_map[:, 1:] != bottom_map[:, :-1])))
        num_faces += 2 * int(np.count_nonzero(pair_y & (bottom_map[1:, :] != bottom_map[:-1, :])))

    # Outer walls: 2 triangles per open pixel side
    num_faces += 2 * int(
        np.count_nonzero(open_front)
        + np.count_nonzero(open_back)
        + np.count_nonzero(open_left)
        + np.count_nonzero(open_right)
    )

    # Internal walls: 2 triangles at every height transition between solid pixels
    num_faces += 2 * int(np.count_nonzero(pair_x & (height_map[:, 1:] != height_map[:, :-1])))
    num_faces += 2 * int(np.count_nonzero(pair_y & (height_map[1:, :] != height_map[:-1, :])))

    # Add faces for mounting features
    if mount_geometry is not None:
        num_faces += len(mount_geometry[1])

    # Create mesh
    stl_mesh: Any = mesh.Mesh(np.zeros(num_faces, dtype=mesh.Mesh.dtype))

    face_idx = 0

    # Create top surface
    for y in range(height):
        for x in range(width):
            if not mask[y, x]:
                continue
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
            if not mask[y, x]:
                continue
            x0, y0 = x * pixel_size, y * pixel_size
            x1, y1 = (x + 1) * pixel_size, (y + 1) * pixel_size
            curr_height = height_map[y, x]

            # Check right neighbor - create vertical wall if heights differ
            if x < width - 1 and mask[y, x + 1]:
                right_height = height_map[y, x + 1]
                if curr_height != right_height:
                    # Create wall between current and right pixel
                    # Wall goes from min height to max height
                    min_h = min(curr_height, right_height)
                    max_h = max(curr_height, right_height)

                    # Two triangles forming a vertical rectangle
                    if curr_height > right_height:
                        # Current is higher, normal points right (toward the lower neighbor)
                        stl_mesh.vectors[face_idx] = np.array([[x1, y0, min_h], [x1, y1, min_h], [x1, y0, max_h]])
                        face_idx += 1

                        stl_mesh.vectors[face_idx] = np.array([[x1, y1, min_h], [x1, y1, max_h], [x1, y0, max_h]])
                        face_idx += 1
                    else:
                        # Right is higher, normal points left (toward the lower neighbor)
                        stl_mesh.vectors[face_idx] = np.array([[x1, y0, min_h], [x1, y0, max_h], [x1, y1, min_h]])
                        face_idx += 1

                        stl_mesh.vectors[face_idx] = np.array([[x1, y0, max_h], [x1, y1, max_h], [x1, y1, min_h]])
                        face_idx += 1

            # Check bottom neighbor - create vertical wall if heights differ
            if y < height - 1 and mask[y + 1, x]:
                bottom_height = height_map[y + 1, x]
                if curr_height != bottom_height:
                    # Create wall between current and bottom pixel
                    # Wall goes from min height to max height
                    min_h = min(curr_height, bottom_height)
                    max_h = max(curr_height, bottom_height)

                    # Two triangles forming a vertical rectangle
                    if curr_height > bottom_height:
                        # Current is higher, normal points toward +Y (the lower neighbor)
                        stl_mesh.vectors[face_idx] = np.array([[x0, y1, min_h], [x0, y1, max_h], [x1, y1, min_h]])
                        face_idx += 1

                        stl_mesh.vectors[face_idx] = np.array([[x0, y1, max_h], [x1, y1, max_h], [x1, y1, min_h]])
                        face_idx += 1
                    else:
                        # Next row is higher, normal points toward -Y (the lower current pixel)
                        stl_mesh.vectors[face_idx] = np.array([[x0, y1, min_h], [x1, y1, min_h], [x0, y1, max_h]])
                        face_idx += 1

                        stl_mesh.vectors[face_idx] = np.array([[x1, y1, min_h], [x1, y1, max_h], [x0, y1, max_h]])
                        face_idx += 1

    # Create outer walls on every solid pixel side that faces empty space, from the
    # pixel's bottom face up to its top face, with normals pointing outward

    for y in range(height):
        for x in range(width):
            if not mask[y, x]:
                continue
            x0, y0 = x * pixel_size, y * pixel_size
            x1, y1 = (x + 1) * pixel_size, (y + 1) * pixel_size
            h = height_map[y, x]
            b = bottom_map[y, x]

            if open_front[y, x]:
                stl_mesh.vectors[face_idx] = np.array([[x0, y0, b], [x1, y0, b], [x0, y0, h]])
                stl_mesh.vectors[face_idx + 1] = np.array([[x1, y0, b], [x1, y0, h], [x0, y0, h]])
                face_idx += 2
            if open_back[y, x]:
                stl_mesh.vectors[face_idx] = np.array([[x0, y1, h], [x1, y1, h], [x0, y1, b]])
                stl_mesh.vectors[face_idx + 1] = np.array([[x1, y1, h], [x1, y1, b], [x0, y1, b]])
                face_idx += 2
            if open_left[y, x]:
                stl_mesh.vectors[face_idx] = np.array([[x0, y0, h], [x0, y1, h], [x0, y0, b]])
                stl_mesh.vectors[face_idx + 1] = np.array([[x0, y1, h], [x0, y1, b], [x0, y0, b]])
                face_idx += 2
            if open_right[y, x]:
                stl_mesh.vectors[face_idx] = np.array([[x1, y0, b], [x1, y1, b], [x1, y0, h]])
                stl_mesh.vectors[face_idx + 1] = np.array([[x1, y1, b], [x1, y1, h], [x1, y0, h]])
                face_idx += 2

    if flat_bottom:
        # Bottom face - solid base covering entire area, facing down
        total_width = width * pixel_size
        total_height = height * pixel_size
        stl_mesh.vectors[face_idx] = np.array([[0, 0, 0], [0, total_height, 0], [total_width, 0, 0]])
        face_idx += 1
        stl_mesh.vectors[face_idx] = np.array(
            [[total_width, 0, 0], [0, total_height, 0], [total_width, total_height, 0]]
        )
        face_idx += 1
    else:
        face_idx = add_recessed_bottom(stl_mesh, face_idx, bottom_map, pixel_size, mask)

    # Add mounting features if requested
    if mount_geometry is not None:
        vertices, triangles = mount_geometry
        for v0, v1, v2 in triangles:
            stl_mesh.vectors[face_idx] = np.array([vertices[v0], vertices[v1], vertices[v2]])
            face_idx += 1

    # Update normals
    stl_mesh.update_normals()

    logger.info(f"Created STL mesh with {face_idx} faces")

    return stl_mesh


def add_recessed_bottom(
    stl_mesh: Any, face_idx: int, bottom_map: np.ndarray, pixel_size: float, mask: np.ndarray
) -> int:
    """Add a bottom surface with recesses (such as back text) to an STL mesh.

    Args:
        stl_mesh: The STL mesh object to add faces to
        face_idx: Current face index to start adding faces
        bottom_map: 2D array of bottom face heights; 0 is flush with the print bed
        pixel_size: Size of each pixel in mm
        mask: Boolean array of solid pixels; empty pixels get no bottom face

    Returns:
        Updated face index after adding bottom faces
    """
    height, width = bottom_map.shape

    for y in range(height):
        for x in range(width):
            if not mask[y, x]:
                continue
            x0, y0 = x * pixel_size, y * pixel_size
            x1, y1 = (x + 1) * pixel_size, (y + 1) * pixel_size
            b = bottom_map[y, x]

            # Floor of this pixel, facing down
            stl_mesh.vectors[face_idx] = np.array([[x0, y0, b], [x0, y1, b], [x1, y0, b]])
            face_idx += 1
            stl_mesh.vectors[face_idx] = np.array([[x1, y0, b], [x0, y1, b], [x1, y1, b]])
            face_idx += 1

            # Wall to the right neighbor, facing into whichever side is recessed deeper
            if x < width - 1 and mask[y, x + 1] and bottom_map[y, x + 1] != b:
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
            if y < height - 1 and mask[y + 1, x] and bottom_map[y + 1, x] != b:
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
    hole_band: float = 2.5,
    tab_thickness: float = 2.0,
) -> tuple[list[list[float]], list[tuple[int, int, int]]]:
    """Generate vertex and triangle data for a keychain mounting tab.

    Args:
        base_width: Width of the QR code base in mm
        base_depth: Depth of the QR code base in mm
        base_height: Height of the base in mm
        hole_diameter: Diameter of the keychain hole in mm
        hole_band: Width of the plastic ring around the hole, including toward the base, in mm
        tab_thickness: Thickness of the tab in mm

    Returns:
        Tuple of (vertices, triangles) where:
        - vertices is a list of [x, y, z] coordinates
        - triangles is a list of (v0, v1, v2) vertex indices
    """
    # The tab is a "D" shape: straight sides rising from the QR base edge, closed
    # by a semicircle concentric with the hole. The band between the hole and the
    # base edge matches the ring, so the plastic is the same width all the way round.
    tab_y_start = base_depth  # Tab starts at the base edge
    hole_radius = hole_diameter / 2
    arc_radius = hole_radius + hole_band
    hole_center_x = base_width / 2
    hole_center_y = tab_y_start + hole_radius + hole_band

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


def generate_arch_mount_geometry(
    base_width: float,
    base_depth: float,
    arch_band: float = 4.0,
    arch_height: float = 12.0,
    arch_padding: float = 2.0,
    thickness: float = MOUNT_THICKNESS_MM,
) -> tuple[list[list[float]], list[tuple[int, int, int]]]:
    """Generate vertex and triangle data for an arch mount spanning the full base width.

    The mount is a solid strip along the base edge (the padding) topped by a half-ellipse
    arch. The arch's outer edges continue the base's sides, and the opening's inner edge is
    the outer curve offset inward by the band, so the band is the same width everywhere.

    Args:
        base_width: Width of the QR code base in mm
        base_depth: Depth of the QR code base in mm (the mount starts at this edge)
        arch_band: Width of the arch band in mm
        arch_height: Total height of the mount above the base edge in mm, padding included
        arch_padding: Height of the solid strip between the base edge and the opening in mm
        thickness: Thickness of the mount in mm

    Returns:
        Tuple of (vertices, triangles) where:
        - vertices is a list of [x, y, z] coordinates
        - triangles is a list of (v0, v1, v2) vertex indices
    """
    a = base_width / 2  # Horizontal semi-axis: the arch spans the full width
    b = arch_height - arch_padding  # Vertical semi-axis
    cx, cy = base_width / 2, base_depth + arch_padding

    # Sample the half-ellipse counter-clockwise from the right leg (0) to the left leg (pi)
    outer_points = []
    inner_points = []
    for i in range(ARCH_SEGMENTS + 1):
        theta = np.pi * i / ARCH_SEGMENTS
        cos_t, sin_t = float(np.cos(theta)), float(np.sin(theta))
        ox, oy = cx + a * cos_t, cy + b * sin_t
        # Outward normal of the ellipse at this point
        nx, ny = cos_t / a, sin_t / b
        length = float(np.hypot(nx, ny))
        outer_points.append((ox, oy))
        inner_points.append((ox - arch_band * nx / length, oy - arch_band * ny / length))

    n = len(outer_points)
    vertices: list[list[float]] = []
    triangles: list[tuple[int, int, int]] = []

    # Vertex layout: outer top, outer bottom, inner top, inner bottom, then the two
    # base-edge corners of the padding strip (top, bottom) on each side
    outer_top, outer_bottom, inner_top, inner_bottom = 0, n, 2 * n, 3 * n
    for z in (thickness, 0.0):
        vertices.extend([x, y, z] for x, y in outer_points)
    for z in (thickness, 0.0):
        vertices.extend([x, y, z] for x, y in inner_points)
    right_top, right_bottom, left_top, left_bottom = 4 * n, 4 * n + 1, 4 * n + 2, 4 * n + 3
    vertices.extend(
        [
            [base_width, base_depth, thickness],
            [base_width, base_depth, 0.0],
            [0.0, base_depth, thickness],
            [0.0, base_depth, 0.0],
        ]
    )

    for i in range(n - 1):
        j = i + 1
        # Band top (normal +Z) and bottom (normal -Z)
        triangles.append((outer_top + i, outer_top + j, inner_top + j))
        triangles.append((outer_top + i, inner_top + j, inner_top + i))
        triangles.append((outer_bottom + i, inner_bottom + j, outer_bottom + j))
        triangles.append((outer_bottom + i, inner_bottom + i, inner_bottom + j))
        # Outer wall, normal pointing away from the arch
        triangles.append((outer_bottom + i, outer_bottom + j, outer_top + j))
        triangles.append((outer_bottom + i, outer_top + j, outer_top + i))
        # Inner wall, normal pointing into the opening
        triangles.append((inner_top + i, inner_top + j, inner_bottom + i))
        triangles.append((inner_top + j, inner_bottom + j, inner_bottom + i))

    # Padding strip: base edge -> right leg -> opening floor -> left leg, counter-clockwise
    o_right, o_left = 0, n - 1  # Outer curve ends, at the top of the strip
    i_right, i_left = 0, n - 1  # Inner curve ends, at the opening floor
    for is_top in (True, False):
        r = right_top if is_top else right_bottom
        lft = left_top if is_top else left_bottom
        o = outer_top if is_top else outer_bottom
        inn = inner_top if is_top else inner_bottom
        faces = [
            (lft, r, inn + i_right),
            (r, o + o_right, inn + i_right),
            (lft, inn + i_right, inn + i_left),
            (lft, inn + i_left, o + o_left),
        ]
        # Top faces wind counter-clockwise (normal +Z); bottom faces are reversed (normal -Z)
        triangles.extend(f if is_top else (f[0], f[2], f[1]) for f in faces)

    # Strip walls: base edge (normal -Y), right side (+X), left side (-X), opening floor (+Y)
    triangles.append((left_bottom, right_bottom, right_top))
    triangles.append((left_bottom, right_top, left_top))
    triangles.append((right_bottom, outer_bottom + o_right, outer_top + o_right))
    triangles.append((right_bottom, outer_top + o_right, right_top))
    triangles.append((outer_bottom + o_left, left_bottom, left_top))
    triangles.append((outer_bottom + o_left, left_top, outer_top + o_left))
    triangles.append((inner_bottom + i_right, inner_bottom + i_left, inner_top + i_left))
    triangles.append((inner_bottom + i_right, inner_top + i_left, inner_top + i_right))

    return vertices, triangles


def generate_mount_geometry(
    mount_type: str | None,
    base_width: float,
    base_depth: float,
    hole_diameter: float = 4.0,
    hole_band: float = 2.5,
    arch_band: float = 4.0,
    arch_height: float = 12.0,
    arch_padding: float = 2.0,
) -> tuple[list[list[float]], list[tuple[int, int, int]]] | None:
    """Generate the geometry for the requested mount, attached to the top (high y) edge of the base.

    Args:
        mount_type: 'keychain', 'arch' or None
        base_width: Width of the QR code base in mm
        base_depth: Depth of the QR code base in mm
        hole_diameter: Keychain hole diameter in mm
        hole_band: Width of the plastic ring around the keychain hole in mm
        arch_band: Width of the arch band in mm
        arch_height: Total height of the arch mount above the base edge in mm
        arch_padding: Height of the solid strip under the arch opening in mm

    Returns:
        (vertices, triangles), or None when no mount is requested
    """
    if mount_type == "keychain":
        return generate_keychain_mount_geometry(
            base_width, base_depth, 0.0, hole_diameter, hole_band, tab_thickness=MOUNT_THICKNESS_MM
        )
    if mount_type == "arch":
        return generate_arch_mount_geometry(base_width, base_depth, arch_band, arch_height, arch_padding)
    return None


def add_mount_3mf(
    mesh_object: Any,
    mount_geometry: tuple[list[list[float]], list[tuple[int, int, int]]],
    material_group: Any | None,
    material_id: int | None,
    triangle_properties: list | None,
) -> None:
    """Add a mount's geometry to a 3MF mesh object.

    Args:
        mesh_object: The 3MF mesh object to add mount to
        mount_geometry: (vertices, triangles) from generate_mount_geometry
        material_group: Material group for coloring (None if separate components)
        material_id: Material ID for the mount
        triangle_properties: List to append triangle properties to
    """
    vertices, triangles = mount_geometry

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
    base_size_mm: tuple[float, float] = (50.0, 50.0),
    base_height_mm: float = 2.0,
    qr_height_mm: float = 2.0,
    invert: bool = False,
    multi_layer: bool = False,
    layer_heights: list[float] | None = None,
    has_frame: bool = False,
    mount_type: str | None = None,
    hole_diameter: float = 4.0,
    hole_band: float = 2.5,
    arch_band: float = 4.0,
    arch_height: float = 12.0,
    arch_padding: float = 2.0,
    corner_radius: float = 0.0,
    back_text: str | None = None,
    back_text_size_mm: float = 4.0,
    back_text_depth_mm: float = 0.6,
    warnings: list[str] | None = None,
) -> bytes:
    """Convert a QR code image to a binary STL model.

    This function uses an optimized surface-based approach for STL generation:
    - Creates a complete top surface mesh (2 triangles per pixel)
    - Adds internal walls between pixels of different heights
    - Includes outer walls and a solid bottom face
    - Results in efficient meshes suitable for 3D printing

    The STL format contains only geometry data (no color information) and is
    optimized for single-material 3D printing. The internal walls ensure proper
    slicing behavior in 3D printing software.

    Args:
        qr_image: PIL Image of the QR code
        base_size_mm: Size of the base in mm (width, height)
        base_height_mm: Height of the base layer in mm
        qr_height_mm: Additional height for QR code modules in mm
        invert: If True, white areas are raised instead of black
        multi_layer: If True, create multiple distinct layer heights
        layer_heights: List of layer heights [base, qr, frame] in mm
        has_frame: If True, indicates the image has a frame to be rendered at a different height
        mount_type: Type of mounting feature ('keychain', 'arch' or None)
        hole_diameter: Diameter of the keychain hole in mm
        hole_band: Width of the plastic ring around the keychain hole in mm
        arch_band: Width of the arch band in mm
        arch_height: Total height of the arch mount above the base edge in mm
        arch_padding: Height of the solid strip under the arch opening in mm
        corner_radius: Radius of the base plate's rounded corners in mm (0 for square corners).
            With an arch mount the top corners stay square, since the arch continues the sides.
        back_text: Optional text recessed (mirrored) into the bottom face
        back_text_size_mm: Height of capital letters in the back text in mm
        back_text_depth_mm: How deep the back text is recessed in mm
        warnings: Optional list that receives non-fatal messages (e.g. back text shrunk)

    Returns:
        Binary STL file contents

    Raises:
        QRModelError: If the back text is too deep for the base or does not fit
    """
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

    # Recess back text into the bottom face, keeping at least 0.4mm of material above it
    bottom_map = None
    if back_text:
        thinnest = float(np.min(height_map))
        if back_text_depth_mm > thinnest - 0.4:
            raise QRModelError(
                f"Back text depth {back_text_depth_mm}mm is too deep for a {thinnest}mm base. "
                f"Use a depth of at most {thinnest - 0.4:.1f}mm or a thicker base."
            )
        text_mask = render_back_text_mask(back_text, height_map.shape, pixel_size, back_text_size_mm, warnings)
        bottom_map = np.where(text_mask, back_text_depth_mm, 0.0)

    # Create STL mesh
    mount_geometry = generate_mount_geometry(
        mount_type,
        img_width * pixel_size,
        img_height * pixel_size,
        hole_diameter,
        hole_band,
        arch_band,
        arch_height,
        arch_padding,
    )
    mask = None
    if corner_radius > 0:
        mask = rounded_rect_mask(height_map.shape, pixel_size, corner_radius, round_top=mount_type != "arch")
    stl_mesh = create_stl_from_heightmap(
        height_map,
        pixel_size=pixel_size,
        mount_geometry=mount_geometry,
        bottom_map=bottom_map,
        mask=mask,
    )

    # Write binary STL directly (80-byte header, triangle count, 50-byte records). numpy-stl's
    # own writer embeds a timestamp in the header, so identical options would differ byte-wise.
    buffer = io.BytesIO()
    buffer.write(b"bigtrooper_qr".ljust(80, b" "))
    buffer.write(np.array([len(stl_mesh.data)], dtype="<u4").tobytes())
    buffer.write(stl_mesh.data.astype(stl_mesh.data.dtype.newbyteorder("<")).tobytes())

    if multi_layer and layer_heights:
        total_height = max(layer_heights)
    else:
        total_height = base_height_mm + qr_height_mm

    logger.info(f"Model dimensions: {base_size_mm[0]:.1f} x {base_size_mm[1]:.1f} x {total_height:.1f} mm")

    if multi_layer and layer_heights:
        logger.info(f"Multi-layer heights: {', '.join(f'{h:.1f}mm' for h in layer_heights)}")

    if mount_type:
        logger.info(f"Added {mount_type} mount")

    if back_text:
        logger.info(f"Recessed back text {back_text_depth_mm:.1f}mm into the bottom face")

    return buffer.getvalue()


def convert_qr_to_3mf(
    qr_image: Image.Image,
    base_size_mm: tuple[float, float] = (50.0, 50.0),
    base_height_mm: float = 2.0,
    qr_height_mm: float = 2.0,
    invert: bool = False,
    base_color: str = "white",
    qr_color: str = "black",
    separate_components: bool = False,
    mount_type: str | None = None,
    hole_diameter: float = 4.0,
    hole_band: float = 2.5,
    arch_band: float = 4.0,
    arch_height: float = 12.0,
    arch_padding: float = 2.0,
    corner_radius: float = 0.0,
) -> bytes:
    """Convert a QR code image to a 3MF model with color support using lib3mf.

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
        base_size_mm: Size of the base in mm (width, height)
        base_height_mm: Height of the base layer in mm
        qr_height_mm: Additional height for QR code modules in mm
        invert: If True, white areas are raised instead of black
        base_color: Color name or hex code for the base/background
        qr_color: Color name or hex code for the QR modules
        separate_components: If True, create separate objects for each color
        mount_type: Type of mounting feature ('keychain', 'arch' or None)
        hole_diameter: Diameter of the keychain hole in mm
        hole_band: Width of the plastic ring around the keychain hole in mm
        arch_band: Width of the arch band in mm
        arch_height: Total height of the arch mount above the base edge in mm
        arch_padding: Height of the solid strip under the arch opening in mm
        corner_radius: Radius of the base plate's rounded corners in mm (0 for square corners).
            With an arch mount the top corners stay square, since the arch continues the sides.

    Returns:
        3MF file contents
    """
    if not HAS_LIB3MF:
        raise ImportError("lib3mf is required for colored 3MF export. Install with: pip install lib3mf")

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

    # Add the base plate: its outline (square or rounded corners) extruded to the base height
    base_width = img_width * pixel_size
    base_depth = img_height * pixel_size

    # Choose which mesh to add base to
    base_target_mesh = base_mesh if separate_components else mesh_object

    # Vertex layout: bottom center, top center, outline at z=0, outline at the base height.
    # The outline is convex, so both caps are fans around the center.
    outline = rounded_rect_outline(base_width, base_depth, corner_radius, round_top=mount_type != "arch")
    n = len(outline)
    base_vertices = []
    for vx, vy, vz in [
        (base_width / 2, base_depth / 2, 0.0),
        (base_width / 2, base_depth / 2, base_height_mm),
        *[(x, y, 0.0) for x, y in outline],
        *[(x, y, base_height_mm) for x, y in outline],
    ]:
        position = lib3mf.Position()
        position.Coordinates[0] = float(vx)
        position.Coordinates[1] = float(vy)
        position.Coordinates[2] = float(vz)
        base_vertices.append(base_target_mesh.AddVertex(position))

    # The outline runs counter-clockwise seen from above
    bottom, top = 2, 2 + n
    base_triangle_indices = []
    for i in range(n):
        j = (i + 1) % n
        base_triangle_indices.append((0, bottom + j, bottom + i))  # Bottom cap, normal -Z
        base_triangle_indices.append((1, top + i, top + j))  # Top cap, normal +Z
        base_triangle_indices.append((bottom + i, bottom + j, top + j))  # Side wall, normal outward
        base_triangle_indices.append((bottom + i, top + j, top + i))

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

    # Add mounting features if requested, in the base color
    mount_geometry = generate_mount_geometry(
        mount_type, base_width, base_depth, hole_diameter, hole_band, arch_band, arch_height, arch_padding
    )
    if mount_geometry is not None:
        add_mount_3mf(
            base_mesh if separate_components else mesh_object,
            mount_geometry,
            material_group if not separate_components else None,
            base_material_id if not separate_components else None,
            triangle_properties if not separate_components else None,
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

    # Serialize to memory
    writer = model.QueryWriter("3mf")  # type: ignore[attr-defined]
    model_bytes = bytes(writer.WriteToBuffer())

    total_height = base_height_mm + qr_height_mm
    logger.info(f"Model dimensions: {base_size_mm[0]:.1f} x {base_size_mm[1]:.1f} x {total_height:.1f} mm")
    logger.info(f"Colors: base={base_color}, QR={qr_color}")

    if mount_type:
        logger.info(f"Added {mount_type} mount")

    return model_bytes
