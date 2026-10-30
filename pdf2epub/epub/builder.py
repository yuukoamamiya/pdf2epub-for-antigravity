"""
EPUB builder for creating EPUB file structure.

This module handles all EPUB-specific file generation including:
- Structural files (content.opf, toc.ncx, etc.)
- Stylesheets
- Final EPUB packaging
"""

import os
import html
import tempfile
import uuid
import zipfile
from pathlib import Path
from datetime import datetime
from typing import Dict, List, Optional, Any
from loguru import logger



def localized_toc_label(language: str) -> str:
    """Return the reader-facing table-of-contents label."""
    normalized = (language or "en").lower().replace("_", "-")
    if normalized == "chinese" or normalized.startswith("zh"):
        return "目录"
    if normalized == "japanese" or normalized.startswith("ja"):
        return "目次"
    return "Table of Contents"


class EpubBuilder:
    """Handles creation of all EPUB structural files and final packaging."""

    def __init__(self, config, converter=None):
        """
        Initialize the EpubBuilder.

        Args:
            config: EpubConfig instance with all settings
            converter: Optional ContentConverter instance for file naming helpers
        """
        self.config = config
        self.converter = converter
        # The NCX and OPF must identify the same publication.
        self.uid = str(uuid.uuid4())
    
    def create_cover_html(self, cover_image_filename: str, output_path: Path) -> bool:
        """
        Create the cover HTML page.
        
        Args:
            cover_image_filename: Name of the cover image file
            output_path: Path to save the cover HTML
            
        Returns:
            True if successful
        """
        try:
            # Get the language from config
            language = self.config.language if hasattr(self.config, 'language') else 'en'
            lang_class = f"lang-{language}"
            
            cover_html = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE html PUBLIC "-//W3C//DTD XHTML 1.1//EN" "http://www.w3.org/TR/xhtml11/DTD/xhtml11.dtd">
<html xmlns="http://www.w3.org/1999/xhtml" xml:lang="{language}" lang="{language}">
<head>
    <title>Cover</title>
    <link rel="stylesheet" href="../stylesheet.css" type="text/css"/>
</head>
<body class="{lang_class}">
    <div class="cover">
        <img src="../images/{cover_image_filename}" alt="{html.escape(self.config.book_title)} Cover" />
    </div>
</body>
</html>"""
            
            with open(output_path, "w", encoding="utf-8") as f:
                f.write(cover_html)
            
            logger.success(f"Created cover HTML: {output_path}")
            return True
        except Exception as e:
            logger.error(f"Failed to create cover HTML: {e}")
            return False
    
    def create_stylesheet(self, output_path: Path) -> bool:
        """
        Create the EPUB stylesheet.
        
        Args:
            output_path: Path to save the stylesheet
            
        Returns:
            True if successful
        """
        try:
            # Load stylesheet from resources directory
            resources_dir = Path(__file__).parent / "resources"
            stylesheet_path = resources_dir / "stylesheet.css"
            
            if stylesheet_path.exists():
                # Use the elegant stylesheet from resources
                with open(stylesheet_path, "r", encoding="utf-8") as f:
                    stylesheet = f.read()
                logger.info(f"Loaded stylesheet from {stylesheet_path}")
            else:
                # Fallback to a minimal default stylesheet if file not found
                logger.warning(f"Stylesheet not found at {stylesheet_path}, using default")
                stylesheet = """/* Default EPUB Stylesheet */
@namespace h "http://www.w3.org/1999/xhtml";

body {
    font-family: Georgia, serif;
    line-height: 1.6;
    margin: 1em;
}

h1 { font-size: 2em; margin: 1em 0; page-break-before: always; }
h2 { font-size: 1.5em; margin: 0.8em 0; }
h3 { font-size: 1.2em; margin: 0.6em 0; }
p { margin: 0.8em 0; }
a { text-decoration: none; }
.toc { page-break-after: always; }
.cover { text-align: center; page-break-after: always; }
"""
            
            with open(output_path, "w", encoding="utf-8") as f:
                f.write(stylesheet)
            
            logger.success(f"Created stylesheet: {output_path}")
            return True
        except Exception as e:
            logger.error(f"Failed to create stylesheet: {e}")
            return False
    
    def create_container_xml(self, output_path: Path) -> bool:
        """
        Create the container.xml file.
        
        Args:
            output_path: Path to save the container.xml
            
        Returns:
            True if successful
        """
        try:
            container_xml = """<?xml version="1.0" encoding="UTF-8"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
    <rootfiles>
        <rootfile full-path="content.opf" media-type="application/oebps-package+xml"/>
    </rootfiles>
</container>"""
            
            output_path.parent.mkdir(parents=True, exist_ok=True)
            
            with open(output_path, "w", encoding="utf-8") as f:
                f.write(container_xml)
            
            logger.success(f"Created container.xml: {output_path}")
            return True
        except Exception as e:
            logger.error(f"Failed to create container.xml: {e}")
            return False
    
    def create_mimetype(self, output_path: Path) -> bool:
        """
        Create the mimetype file.
        
        Args:
            output_path: Path to save the mimetype file
            
        Returns:
            True if successful
        """
        try:
            with open(output_path, "w", encoding="utf-8") as f:
                f.write("application/epub+zip")
            
            logger.success(f"Created mimetype: {output_path}")
            return True
        except Exception as e:
            logger.error(f"Failed to create mimetype: {e}")
            return False
    
    def create_content_opf(self, structure: Dict[str, Any], epub_dir: Path, 
                           output_path: Path, cover_image: Optional[str] = None,
                           all_html_files: Optional[List[str]] = None) -> bool:
        """
        Create the content.opf file with manifest and spine.
        
        Args:
            structure: Book structure dictionary
            epub_dir: EPUB directory containing all files
            output_path: Path to save the content.opf
            cover_image: Cover image filename
            all_html_files: List of all HTML files created (for multi-part chapters)
            
        Returns:
            True if successful
        """
        try:
            # Get current date
            date = datetime.now().strftime("%Y-%m-%dT%H:%M:%SZ")
            
            # Start OPF document
            author = getattr(self.config, "author", "") or "Unknown"
            publisher = getattr(self.config, "publisher", "") or ""
            publisher_element = (
                f"        <dc:publisher>{html.escape(publisher)}</dc:publisher>\n"
                if publisher
                else ""
            )
            opf = f"""<?xml version="1.0" encoding="UTF-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="2.0" unique-identifier="BookId">
    <metadata xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:opf="http://www.idpf.org/2007/opf">
        <dc:title>{html.escape(self.config.book_title or "Unknown")}</dc:title>
        <dc:creator opf:role="aut">{html.escape(author)}</dc:creator>
{publisher_element}        <dc:language>{self.config.language}</dc:language>
        <dc:identifier id="BookId" opf:scheme="UUID">{self.uid}</dc:identifier>
        <dc:date>{date}</dc:date>
        <meta name="generator" content="PDF2EPUB v3"/>
        {f'<meta name="cover" content="cover-image"/>' if cover_image else ''}
    </metadata>

    <manifest>
        <item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>
        <item id="stylesheet" href="stylesheet.css" media-type="text/css"/>
        {f'<item id="cover" href="text/cover.html" media-type="application/xhtml+xml"/>' if cover_image else ''}
        <item id="toc" href="text/toc.html" media-type="application/xhtml+xml"/>
"""
            
            # Add cover image to manifest
            if cover_image:
                opf += f"""        <item id="cover-image" href="images/{cover_image}" media-type="image/jpeg"/>
"""
            
            # Add HTML files to manifest
            if all_html_files:
                # Use provided list of HTML files (for proper ordering with multi-part chapters)
                for html_file in all_html_files:
                    if html_file is None:
                        continue
                    item_id = html_file.replace(".html", "").replace(" ", "_").replace("-", "_")
                    opf += f"""        <item id="{item_id}" href="text/{html_file}" media-type="application/xhtml+xml"/>
"""
            else:
                # Fallback to structure-based manifest
                # Add front matter if exists
                if structure.get("front_matter"):
                    opf += """        <item id="front_matter" href="text/front_matter.html" media-type="application/xhtml+xml"/>
"""
                
                # Add chapters to manifest
                for i, chapter in enumerate(structure.get("chapters", []), 1):
                    chapter_id = f"chapter_{chapter.get('index', i)}"
                    chapter_file = f"{chapter_id}.html"
                    opf += f"""        <item id="{chapter_id}" href="text/{chapter_file}" media-type="application/xhtml+xml"/>
"""
                
                # Add back matter if exists
                if structure.get("back_matter"):
                    opf += """        <item id="back_matter" href="text/back_matter.html" media-type="application/xhtml+xml"/>
"""
            
            # Add images to manifest
            images_dir = epub_dir / "images"
            if images_dir.exists():
                for img_file in sorted(images_dir.iterdir()):
                    if img_file.is_file() and img_file.name != cover_image:
                        img_id = img_file.stem.replace(" ", "_").replace("-", "_")
                        media_type = self._get_image_media_type(img_file.suffix)
                        if media_type:
                            opf += f"""        <item id="img_{img_id}" href="images/{img_file.name}" media-type="{media_type}"/>
"""
            
            opf += """    </manifest>

    <spine toc="ncx">
"""
            if cover_image:
                opf += """        <itemref idref="cover" linear="no"/>
"""
            opf += """        <itemref idref="toc"/>
"""
            
            # Build spine based on all_html_files if provided
            if all_html_files:
                # Use the exact order of HTML files (important for multi-part chapters)
                for html_file in all_html_files:
                    if html_file is None:
                        continue
                    if html_file != "cover.html" and html_file != "toc.html":  # Already added
                        item_id = html_file.replace(".html", "").replace(" ", "_").replace("-", "_")
                        opf += f"""        <itemref idref="{item_id}"/>
"""
            else:
                # Fallback to structure-based spine
                # Add front matter to spine
                if structure.get("front_matter"):
                    opf += """        <itemref idref="front_matter"/>
"""
                
                # Add chapters to spine
                for i, chapter in enumerate(structure.get("chapters", []), 1):
                    chapter_id = f"chapter_{chapter.get('index', i)}"
                    opf += f"""        <itemref idref="{chapter_id}"/>
"""
                
                # Add back matter to spine
                if structure.get("back_matter"):
                    opf += """        <itemref idref="back_matter"/>
"""
            
            opf += """    </spine>
</package>"""
            
            with open(output_path, "w", encoding="utf-8") as f:
                f.write(opf)
            
            logger.success(f"Created content.opf: {output_path}")
            return True
        except Exception as e:
            import traceback
            logger.error(f"Failed to create content.opf: {e}")
            logger.error(traceback.format_exc())
            return False
    
    def _get_image_media_type(self, suffix: str) -> Optional[str]:
        """Get the media type for an image file extension."""
        media_types = {
            ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg",
            ".png": "image/png",
            ".gif": "image/gif",
            ".svg": "image/svg+xml",
            ".webp": "image/webp"
        }
        return media_types.get(suffix.lower())
    
    def create_epub(self, epub_dir: Path, output_path: Path) -> bool:
        """
        Create the final EPUB file by zipping the directory.
        
        Args:
            epub_dir: Directory containing all EPUB files
            output_path: Path for the output EPUB file
            
        Returns:
            True if successful
        """
        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary_name = tempfile.mkstemp(
            prefix=f".{output_path.name}.", suffix=".tmp", dir=output_path.parent
        )
        os.close(fd)
        temporary_path = Path(temporary_name)
        try:
            with zipfile.ZipFile(temporary_path, "w", zipfile.ZIP_DEFLATED) as epub:
                # Add mimetype first and uncompressed as required by EPUB.
                mimetype_path = epub_dir / "mimetype"
                if mimetype_path.exists():
                    epub.write(mimetype_path, "mimetype", compress_type=zipfile.ZIP_STORED)
                else:
                    epub.writestr(
                        "mimetype", "application/epub+zip", compress_type=zipfile.ZIP_STORED
                    )

                for root, _dirs, files in os.walk(epub_dir):
                    for file in files:
                        if file != "mimetype":
                            file_path = Path(root) / file
                            arcname = file_path.relative_to(epub_dir)
                            epub.write(file_path, arcname)

            # Confirm the central directory and compressed data are readable
            # before publishing the new archive.
            with zipfile.ZipFile(temporary_path, "r") as epub:
                bad_member = epub.testzip()
                if bad_member is not None:
                    raise zipfile.BadZipFile(f"Corrupt EPUB member: {bad_member}")
            os.replace(temporary_path, output_path)
            logger.success(f"Created EPUB: {output_path}")
            return True
        except BaseException:
            temporary_path.unlink(missing_ok=True)
            raise
