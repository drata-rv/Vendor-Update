import requests
import os
from typing import Optional, Dict

import csv

import os
from pathlib import Path
from typing import List, Generator

def list_files_recursive(directory: str) -> List[str]:
    """
    Recursive approach using os.walk()
    Returns full paths of all files including those in subdirectories
    """
    files = []
    for root, _, filenames in os.walk(directory):
        for filename in filenames:
            full_path = os.path.join(root, filename)
            files.append(full_path)
    return files

def read_csv_to_dict(csv_path: str) -> Dict[str, str]:
    """
    Reads a CSV file with 'ID' and 'Name' columns and returns a dictionary mapping IDs to Names.
    
    Args:
        csv_path: Path to the CSV file
        
    Returns:
        Dictionary with ID as key and Name as value
    """
    id_to_name_map = {}
    
    try:
        with open(csv_path, 'r', encoding='utf-8') as csv_file:
            # Create CSV reader with header support
            csv_reader = csv.DictReader(csv_file)
            
            # Validate required columns exist
            required_headers = {'ID', 'Name'}
            headers = set(csv_reader.fieldnames or [])
            if not required_headers.issubset(headers):
                missing = required_headers - headers
                raise ValueError(f"Missing required columns: {missing}")
            
            # Build the mapping
            for row in csv_reader:
                id_value = row['ID'].strip()
                name_value = row['Name'].strip()
                if id_value:  # Only add if ID is not empty
                    id_to_name_map[id_value] = name_value
                    
        return id_to_name_map
    
    except FileNotFoundError:
        raise FileNotFoundError(f"CSV file not found: {csv_path}")
    except Exception as e:
        raise Exception(f"Error reading CSV file: {str(e)}")

# def main():
#     # Example usage
#     csv_path = 'path/to/your/file.csv'
#     
#     try:
#         id_name_mapping = read_csv_to_dict(csv_path)
#         
#         # Print the results
#         print("ID to Name mapping:")
#         for id_value, name in id_name_mapping.items():
#             print(f"ID: {id_value} -> Name: {name}")
#             
#         # Example of looking up a specific ID
#         sample_id = next(iter(id_name_mapping))  # Get first ID
#         print(f"\nLooking up ID '{sample_id}': {id_name_mapping[sample_id]}")
#         
#     except Exception as e:
#         print(f"Error: {e}")


class DrataVendorAPI:
    def __init__(self, base_url: str, api_key: str):
        self.base_url = base_url.rstrip('/')
        self.headers = {
            'Authorization': f'Bearer {api_key}',
            'Accept': 'application/json'
        }

    def find_vendor_by_name(self, vendor_name: str) -> Optional[Dict]:
        """Find a vendor by name"""
        endpoint = f"{self.base_url}/public/vendors"
        params = {'q': vendor_name}
        
        response = requests.get(endpoint, headers=self.headers, params=params)
        response.raise_for_status()
        
        vendors = response.json().get('data', [])
        return vendors[0] if vendors else None

    def get_mimetype(self, extension):
        e = extension.strip(".").lower()
        if e == "png":
            return 'image/png'
        if e == "docx":
            return 'application/vnd.openxmlformats-officedocument.wordprocessingml.document'
        if e == "xlsx":
            return 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
        return 'application/'+e

    def upload_vendor_document(self, vendor_id: int, file_path: str, document_type: str = 'COMPLIANCE_REPORT') -> Dict:
        """Upload a document to a vendor"""
        endpoint = f"{self.base_url}/public/vendors/{vendor_id}/documents"
        
        if not os.path.exists(file_path):
            raise FileNotFoundError(f"File not found: {file_path}")

        basename, extension = os.path.splitext(file_path)
        files = {
            'file': (os.path.basename(file_path), open(file_path, 'rb'), self.get_mimetype(extension))
        }
        
        data = {
            'type': document_type
        }

        response = requests.post(endpoint, headers=self.headers, files=files, data=data)
        response.raise_for_status()
        return response.json()
    
    # def upload_vendor_questionnaire(self, vendor_id: int, file_path: str, document_type: str = 'COMPLIANCE_REPORT') -> Dict:
    #     """Upload a document to a vendor"""
    #     endpoint = f"{self.base_url}/public/vendors/{vendor_id}/questionnaires/manual"
    #     
    #     if not os.path.exists(file_path):
    #         raise FileNotFoundError(f"File not found: {file_path}")
    #     
    #     basename, extension = os.path.splitext(file_path)
    #     files = {
    #         'file': (os.path.basename(file_path), open(file_path, 'rb'), self.get_mimetype(extension))
    #     }
    #     
    #     response = requests.post(endpoint, headers=self.headers, files=files)
    #     response.raise_for_status()
    #     return response.json()

def main():
    # Initialize the API client
    api_key = os.getenv('DRATA_API_KEY', 'XXX')
    base_url = os.getenv('DRATA_API_URL', 'http://localhost:4000')
    id_name_mapping = read_csv_to_dict('vendors.csv')
    
    if not api_key:
        raise ValueError("DRATA_API_KEY environment variable is required")

    client = DrataVendorAPI(base_url, api_key)

    for id_value, vendor_name in id_name_mapping.items():
        document_path = "downloads/vendor"+id_value
        
        files = list_files_recursive(document_path)
        for f in files:
            if ".zip" in f or "__MACOSX" in f or f.endswith(".asc"):
                print("skipping zip file "+f)
                continue
            if ".png" not in f:
                continue
            document_type = 'COMPLIANCE_REPORT'
            if "SOC" in f:
                document_type = 'SOC_DOCUMENT'
            if "policy" in f.lower() or "xls" in f.lower():
                document_type = 'QUESTIONNAIRE_ATTACHMENT'
            try:
                # Find vendor
                vendor = client.find_vendor_by_name(vendor_name)
                if not vendor:
                    print(f"Vendor '{vendor_name}' not found")
                    continue
                print(f"Uploading file {f} to vendor {vendor['name']}")

                result = None        
                # Upload document
                result = client.upload_vendor_document(
                    vendor_id=vendor['id'],
                    file_path=f,
                    document_type=document_type
                )
                print(f"Document uploaded successfully: {result}")
        
            except requests.exceptions.RequestException as e:
                print(f"API request failed: {e}")
            except Exception as e:
                print(f"Error: {e}")

if __name__ == "__main__":
    main()
