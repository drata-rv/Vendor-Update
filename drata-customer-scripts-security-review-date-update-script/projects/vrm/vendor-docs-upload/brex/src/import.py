import csv
from typing import Dict, Any
import logging
from datetime import datetime
import traceback
import http.client
# log = logging.getLogger('urllib3')
# log.setLevel(logging.DEBUG)
# http.client.HTTPConnection.debuglevel = 1
import requests
import re


public='https://public-api.drata.com/public/'

class DrataVendorImporter:
    def __init__(self, api_key: str="XXX", base_url: str = "http://localhost:4000/public"):
        self.api_key = api_key
        self.base_url = base_url
        self.headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json"
        }
        
        # Setup logging
        logging.basicConfig(
            level=logging.INFO,
            format='%(asctime)s - %(levelname)s - %(message)s',
            handlers=[
                logging.FileHandler('vendor_import.log'),
                logging.StreamHandler()
            ]
        )
        self.logger = logging.getLogger(__name__)  
        
    def parse_url(self, row):
        website = row.get("Website", None)
        if website:
            urls = re.split(r'\s+|\n', website)
            website = urls[0]
            if "http://" not in website and "https://" not in website:
                return "https://"+website
            return website
        domain = row.get("Domain",None)
        if domain:
            return "https://"+domain
        return None
    
    def parse_privacy(self, row):
        privacy = row.get("Privacy Policy Link",None)
        if privacy:
            if "http://" not in privacy and "https://" not in privacy:
                return "https://"+privacy
            return privacy
        return None
    
    def has_pii(self, row):
        #need to handle the No collected case
        data_collected = row.get("Data Collected","")
        if "Customer" in data_collected or "Marketing" in data_collected or "Employee" in data_collected or "Payment" in data_collected or "Client" in data_collected or "Email" in data_collected:
            return True
        return False
    
    def data_accessed_or_processed(self, row):
        # "GENERAL" "PUBLIC" "CONTROLLED_UNCLASSIFIED" "FINANCIAL" "PROPRIETARY" "EMPLOYEE_PERSONNEL" "PERSONAL_IDENTIFIABLE_INFORMATION" "PROTECTED_HEALTH_INFORMATION" "OTHER_PERSONAL_OR_SENSITIVE" "CARDHOLDER_DATA"
        # Need to handle the No case
        data_collected = row.get("Data Collected","")
        data_accessed_or_processed_list = []
        if "Employee" in data_collected:
            data_accessed_or_processed_list.append("EMPLOYEE_PERSONNEL")
        if "Client" in data_collected:
            data_accessed_or_processed_list.append("PROTECTED_HEALTH_INFORMATION")
        if "PCI" in data_collected:
            data_accessed_or_processed_list.append("CARDHOLDER_DATA")
        if self.has_pii(row):
            data_accessed_or_processed_list.append("PERSONAL_IDENTIFIABLE_INFORMATION")
        if len(data_accessed_or_processed_list) == 0:
            return None
        return data_accessed_or_processed_list
            
    def parse_email(self,row):
        # TODO how to handle multiples
        contact = row.get("External Vendor Contact", None)
        if contact is None:
            return None
        emails = re.split(r'\s+|\n', contact)
        return emails[0]
    
    def parse_type(self, row):
        # "VENDOR" "SUPPLIER" "CONTRACTOR" "PARTNER" "OTHER" "NONE"
        return "VENDOR"
    
    def parse_data_stored(self, row):
        data_stored = row.get("Data Collected","")
        if len(data_stored) == 0:
            return "Unknown"
        return data_stored
        

    def transform_vendor_data(self, row: Dict[str, Any]) -> Dict[str, Any]:
        """Transform CSV row data into Drata API format"""
        
        # Map risk levels
        risk_mapping = {
            "Low Risk": "LOW",
            "Medium Risk": "MODERATE",
            "High Risk": "HIGH",
        }
        
        # Map status
        # Do we want to use Stage of Review  to influence this?
        status_mapping = {
            "Active": "ACTIVE",
            "Archived": "ARCHIVED",
            "Pending": "PROSPECTIVE",
            "approved": "APPROVED",
            "in-review": "UNDER_REVIEW",
            "requested": "PROSPECTIVE",
            "rejected": "REJECTED",
        }
        
        operationalImpact = {"Critical": "CRITICAL",
            "High": "IMPORTANT",
            "Medium": "NORMAL",
            "Low": "LOW"}
        impactLevel= {"Critical": "CRITICAL",
            "High": "MAJOR",
            "Medium": "MODERATE",
            "Low": "MINOR"}

        criticality = row.get('Criticality', None)
        computedOperationalImpact = None
        computedImpactLevel = None
        computedCritical = None
        if criticality:
            if criticality == 'Critical':
                computedCritical = True
            else:
                computedCritical = False
            computedImpactLevel = impactLevel[criticality]
            computedOperationalImpact = operationalImpact[criticality]
            
        computedStatus = 'ACTIVE'
        if row.get('Stage of Review') == 'In Review':
            computedStatus = 'UNDER_REVIEW'
        else:
            computedStatus = status_mapping.get(row.get('Status', 'Active'), 'ACTIVE')
            

        # left hand side before the : is the name of the Drata field, the right hand side is the mapped value from OneTrust
        return {
            "name": row['Name'],
            "url": self.parse_url(row),
            "status": computedStatus,
            "risk": None, # All nones are todo, need to know how to compute from their risk scores
            "operationalImpact":computedOperationalImpact,
            "impactLevel": computedImpactLevel,
            "critical": computedCritical,
            "isSubProcessor": False, # TODO
            "isSubProcessorActive": False, # TODO
            "location": row.get('Location',None),
            "servicesProvided": row.get('Description'),
            "notes": row.get('Notes','')+"\n"+row.get('Additional Comment(s)', ''),
            "contactAtVendor": None,
            "contactsEmail": self.parse_email(row),
            "dataStored": self.parse_data_stored(row),
            "privacyUrl": self.parse_privacy(row),
            "dataAccessedOrProcessedList": self.data_accessed_or_processed(row),
            "hasPii": self.has_pii(row),
            "passwordRequiresMinLength": False,
            "passwordRequiresSymbol": False,
            "passwordMfaEnabled": False,
            "passwordMinLength": 8,
            "passwordRequiresNumber": False,
            "type": self.parse_type(row),
            
            #userId, want to map this to a user in drata Vendor Owner  is the email today row.get('Vendor Owner')
            
            # "customFields": {
            #     "effectiveDate": effective_date,
            #     "lastReviewDate": last_review_date,
            #     "businessSegment": row.get('Business Segment', ''),
            #     "division": row.get('Division', ''),
            #     "owningTeam": row.get('Owning Team', ''),
            #     "rto": row.get('Recovery Time Objective (RTO)', ''),
            #     "rpo": row.get('Recovery Point Objective (RPO)', '')
            # }
        }

    def import_vendors(self, csv_path: str) -> None:
        """Import vendors from CSV file to Drata"""
        success_count = 0
        error_count = 0
        
        try:
            with open(csv_path, 'r') as file:
                reader = csv.DictReader(file)
                
                for row in reader:
                    try:
                        # Skip empty rows
                        if not row['Name']:
                            continue
                            
                        vendor_data = self.transform_vendor_data(row)
                        
                        # Post to Drata API
                        response = requests.post(
                            f"{self.base_url}/vendors",
                            headers=self.headers,
                            json=vendor_data
                        )
                        
                        if response.status_code == 201:
                            success_count += 1
                            self.logger.info(f"Successfully imported vendor: {row['Name']}")
                        else:
                            error_count += 1
                            self.logger.error(
                                f"Failed to import vendor {row['Name']}: "
                                f"Status {response.status_code}, "
                                f"Response: {response.text}"
                                f"data: {vendor_data}"
                            )
                            
                    except Exception as e:
                        error_count += 1
                        traceback.print_exc()
                        self.logger.error(f"Error processing vendor {row.get('Name', 'Unknown')}: {str(e)}")
                        
        except Exception as e:
            self.logger.error(f"Error reading CSV file: {str(e)}")
            
        self.logger.info(f"Import completed. Successful: {success_count}, Failed: {error_count}")

if __name__ == "__main__":
    import argparse
    
    parser = argparse.ArgumentParser(description='Import vendors from CSV to Drata')
    parser.add_argument('csv_file', help='Path to the CSV file containing vendor data')
    parser.add_argument('api_key', help='Drata API key')
    parser.add_argument('--base-url', default='http://localhost:4000/public', help='Drata API base URL')
    
    args = parser.parse_args()
    
    importer = DrataVendorImporter(args.api_key, args.base_url)
    importer.import_vendors(args.csv_file)

