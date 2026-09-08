"""
Phase 1: Vision extraction using low-cost model (Claude Haiku or Google Cloud Vision).
This module extracts raw text from betting slip screenshots.
"""
import os
import base64
from typing import Optional
from PIL import Image


class VisionExtractor:
    """Extract text from betting slip images using cost-effective vision models."""
    
    def __init__(self, api_key: Optional[str] = None, provider: str = "haiku"):
        """
        Initialize the vision extractor.
        
        Args:
            api_key: API key for the vision service
            provider: 'haiku' (Claude Haiku) or 'gcv' (Google Cloud Vision)
        """
        self.api_key = api_key or os.getenv('ANTHROPIC_API_KEY')
        self.provider = provider
        
        if provider == 'gcv':
            self._init_gcv()
    
    def _init_gcv(self):
        """Initialize Google Cloud Vision client."""
        try:
            from google.cloud import vision
            self.gcv_client = vision.ImageAnnotatorClient()
        except ImportError:
            raise ImportError("google-cloud-vision not installed. Run: pip install google-cloud-vision")
    
    def _encode_image(self, image_path: str) -> str:
        """Encode image to base64."""
        with open(image_path, 'rb') as f:
            return base64.b64encode(f.read()).decode('utf-8')
    
    def extract_text_haiku(self, image_path: str) -> str:
        """Extract text using Claude Haiku (fast, low-cost)."""
        try:
            from anthropic import Anthropic
        except ImportError:
            raise ImportError("anthropic not installed. Run: pip install anthropic")
        
        client = Anthropic(api_key=self.api_key)
        encoded_image = self._encode_image(image_path)
        
        # Optimized prompt for betting slip OCR
        system_prompt = """You are an OCR assistant specialized in extracting text from betting slips.
Extract ALL text exactly as it appears. Include:
- Sportsbook name
- Bet date and time
- All bet details (teams, players, prop types, odds)
- Stake amount and potential payout
- Any status indicators (won/lost/pending)

Return ONLY the raw extracted text. Do not interpret or summarize."""

        response = client.messages.create(
            model="claude-3-haiku-20240307",
            max_tokens=2000,
            system=system_prompt,
            messages=[{
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": "image/png",
                            "data": encoded_image
                        }
                    },
                    {
                        "type": "text",
                        "text": "Extract all text from this betting slip screenshot."
                    }
                ]
            }]
        )
        
        return response.content[0].text
    
    def extract_text_gcv(self, image_path: str) -> str:
        """Extract text using Google Cloud Vision API."""
        from google.cloud import vision
        
        with open(image_path, 'rb') as f:
            content = f.read()
        
        image = vision.Image(content=content)
        response = self.gcv_client.text_detection(image=image)
        texts = response.text_annotations
        
        if texts:
            return texts[0].description
        return ""
    
    def extract_text(self, image_path: str) -> str:
        """
        Extract text from image using configured provider.
        
        Args:
            image_path: Path to the betting slip screenshot
            
        Returns:
            Raw extracted text
        """
        if not os.path.exists(image_path):
            raise FileNotFoundError(f"Image not found: {image_path}")
        
        if self.provider == 'gcv':
            return self.extract_text_gcv(image_path)
        else:
            return self.extract_text_haiku(image_path)


def extract_betting_slip_text(image_path: str, api_key: Optional[str] = None, 
                               provider: str = "haiku") -> str:
    """
    Convenience function to extract text from a betting slip image.
    
    Args:
        image_path: Path to the betting slip screenshot
        api_key: API key for the vision service
        provider: 'haiku' or 'gcv'
        
    Returns:
        Raw extracted text
    """
    extractor = VisionExtractor(api_key=api_key, provider=provider)
    return extractor.extract_text(image_path)
