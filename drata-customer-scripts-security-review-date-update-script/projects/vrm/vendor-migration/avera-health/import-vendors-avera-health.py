import csv
from typing import Dict, Any, Optional, Tuple
import logging
from datetime import datetime, timedelta
import traceback
import http.client
import json
import os
import requests
import re
from urllib.parse import urlparse

public='https://public-api.drata.com/public/v2'

class DrataVendorImporter:
    def __init__(self, api_key: str="XXX", base_url: str = "https://public-api.drata.com/public/v2"):
        self.api_key = api_key
        self.base_url = base_url
        self.headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json"
        }
        
        # Setup logging - will be updated by main if debug flag is set
        logging.basicConfig(
            level=logging.INFO,
            format='%(asctime)s - %(levelname)s - %(message)s',
            handlers=[
                logging.FileHandler('vendor_import.log'),
                logging.StreamHandler()
            ]
        )
        self.logger = logging.getLogger(__name__)
        
    def validate_url(self, url: str, entity_name: str = "Unknown", url_type: str = "URL") -> Optional[str]:
        """
        Validate a URL for format and ensure it starts with https://
        
        Args:
            url: The URL to validate
            entity_name: Name of the entity (vendor) for logging
            url_type: Type of URL for logging (e.g., "Privacy URL", "Website URL")
            
        Returns:
            The validated URL or None if invalid
        """
        if not url or not url.strip():
            return None
            
        url = url.strip()
        
        # Ensure URL starts with https://
        if not url.startswith(('http://', 'https://')):
            url = "https://" + url
        elif url.startswith('http://'):
            url = url.replace('http://', 'https://', 1)
            
        # Validate URL format
        try:
            result = urlparse(url)
            is_valid_url = all([result.scheme, result.netloc])
        except:
            is_valid_url = False
            
        if is_valid_url:
            return url
        else:
            self.logger.warning(f"Invalid {url_type} for {entity_name}: {url}")
            return None

    def parse_url(self, row: Dict[str, Any]) -> Optional[str]:
        """Parse URL from domain of email in Assessment POC field, excluding personal email providers"""
        assessment_poc = row.get("Assessment POC", "").strip()
        if not assessment_poc:
            return None
            
        # Extract email from Assessment POC field
        email_pattern = r'\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b'
        emails = re.findall(email_pattern, assessment_poc)
        
        if not emails:
            return None
            
        # Extract domain from first email found
        email = emails[0]
        domain = email.split('@')[1].lower()
        
        # List of common personal email providers to exclude
        personal_email_domains = {
            'gmail.com', 'yahoo.com', 'hotmail.com', 'outlook.com', 'aol.com',
            'icloud.com', 'me.com', 'mac.com', 'live.com', 'msn.com',
            'comcast.net', 'verizon.net', 'att.net', 'sbcglobal.net',
            'cox.net', 'charter.net', 'earthlink.net', 'juno.com',
            'protonmail.com', 'tutanota.com', 'yandex.com', 'mail.ru'
        }
        
        # Skip if it's a personal email domain
        if domain in personal_email_domains:
            self.logger.debug(f"Skipping personal email domain '{domain}' for vendor {row.get('Vendor', 'Unknown')}")
            return None
        
        # Create URL from domain
        url = f"https://{domain}"
        
        return self.validate_url(
            url,
            entity_name=row.get('Vendor', 'Unknown'),
            url_type="Email Domain URL"
        )

    def parse_privacy_url(self, row: Dict[str, Any]) -> Optional[str]:
        """Parse Privacy Policy Link"""
        privacy = row.get("Privacy Policy Link", "").strip()
        return self.validate_url(
            privacy, 
            entity_name=row.get('Vendor', 'Unknown'), 
            url_type="Privacy URL"
        ) if privacy else None
    
    def parse_status(self, row: Dict[str, Any]) -> str:
        """Map Stage field to Drata status"""
        stage = row.get("Stage", "").strip()
        
        status_mapping = {
            "Active": "ACTIVE",
            "Complete": "APPROVED", 
            "Remediation": "UNDER_REVIEW"
        }
        
        return status_mapping.get(stage, "ACTIVE")  # Default to ACTIVE
    
    def parse_risk(self, row: Dict[str, Any]) -> Optional[str]:
        """Map Risk Rating to Drata risk levels"""
        risk_rating = row.get("Risk Rating", "").strip().lower()
        
        risk_mapping = {
            "low": "LOW",
            "managed": "MODERATE",
            "medium": "MODERATE", 
            "high": "HIGH",
            "very high": "HIGH"
        }
        
        return risk_mapping.get(risk_rating)
    
    def parse_impact_level(self, row: Dict[str, Any]) -> str:
        """Map Impact to Drata impact levels"""
        impact = row.get("Impact", "").strip().lower()
        
        if not impact:
            return "UNSCORED"
            
        impact_mapping = {
            "low": "MINOR",
            "medium": "MODERATE",
            "high": "MAJOR", 
            "very high": "CRITICAL"
        }
        
        return impact_mapping.get(impact, "UNSCORED")
    
    def parse_contact_email(self, row: Dict[str, Any]) -> Optional[str]:
        """Extract email from Assessment POC field"""
        assessment_poc = row.get("Assessment POC", "").strip()
        if not assessment_poc:
            return None
            
        # Simple email extraction - look for email pattern
        email_pattern = r'\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b'
        emails = re.findall(email_pattern, assessment_poc)
        
        return emails[0] if emails else None
    
    def parse_notes(self, row: Dict[str, Any]) -> Optional[str]:
        """Concatenate Notes and Additional Comment(s) fields"""
        notes = row.get("Notes", "").strip()
        additional_comments = row.get("Additional Comment(s)", "").strip()
        
        # Combine notes, handling empty fields
        combined_notes = []
        if notes:
            combined_notes.append(notes)
        if additional_comments:
            combined_notes.append(additional_comments)
            
        return "\n".join(combined_notes) if combined_notes else None

    def get_renewal_date(self, row: Dict[str, Any]) -> str:
        """
        Calculate renewal date based on Results Delivered field
        
        If Results Delivered date is within the last 6 months, set renewal date to one year from that date.
        Otherwise, set renewal date to one year from today.
        """
        results_delivered = row.get("Results Delivered", "").strip()
        
        if not results_delivered:
            # No date provided, use one year from today
            renewal_date = datetime.now() + timedelta(days=365)
            return renewal_date.strftime("%Y-%m-%d")
        
        try:
            # Try to parse the Results Delivered date
            # Handle common date formats, prioritizing M/D/YY format
            date_formats = [
                "%m/%d/%y",      # 7/20/22 (most common format in your data)
                "%m/%d/%Y",      # 01/15/2024
                "%Y-%m-%d",      # 2024-01-15
                "%m-%d-%Y",      # 01-15-2024
                "%d/%m/%Y",      # 15/01/2024
                "%d-%m-%Y",      # 15-01-2024
                "%Y/%m/%d",      # 2024/01/15
                "%B %d, %Y",     # January 15, 2024
                "%b %d, %Y",     # Jan 15, 2024
                "%d %B %Y",      # 15 January 2024
                "%d %b %Y"       # 15 Jan 2024
            ]
            
            results_date = None
            for date_format in date_formats:
                try:
                    results_date = datetime.strptime(results_delivered, date_format)
                    self.logger.debug(f"Successfully parsed date '{results_delivered}' using format '{date_format}'")
                    break
                except ValueError:
                    continue
            
            if results_date is None:
                self.logger.warning(f"Could not parse Results Delivered date '{results_delivered}' for vendor {row.get('Vendor', 'Unknown')}. Using one year from today.")
                renewal_date = datetime.now() + timedelta(days=365)
                return renewal_date.strftime("%Y-%m-%d")
            
            # Check if the results date is within the last 6 months
            six_months_ago = datetime.now() - timedelta(days=180)
            
            if results_date >= six_months_ago:
                # Results delivered within last 6 months - set renewal to one year from results date
                renewal_date = results_date + timedelta(days=365)
                self.logger.debug(f"Results delivered within 6 months ({results_delivered}), setting renewal to {renewal_date.strftime('%Y-%m-%d')}")
            else:
                # Results delivered more than 6 months ago - set renewal to one year from today
                renewal_date = datetime.now() + timedelta(days=365)
                self.logger.debug(f"Results delivered more than 6 months ago ({results_delivered}), setting renewal to one year from today")
            
            return renewal_date.strftime("%Y-%m-%d")
            
        except Exception as e:
            self.logger.warning(f"Error processing Results Delivered date '{results_delivered}' for vendor {row.get('Vendor', 'Unknown')}: {str(e)}. Using one year from today.")
            renewal_date = datetime.now() + timedelta(days=365)
            return renewal_date.strftime("%Y-%m-%d")

    def transform_vendor_data(self, row: Dict[str, Any]) -> Dict[str, Any]:
        """Transform CSV row data into Drata API format"""
        
        # Build the vendor data according to the new mapping
        vendor_data = {
            "name": row.get('Vendor', '').strip(),
            "status": self.parse_status(row),
            "renewalDate": self.get_renewal_date(row),
            "type": "VENDOR",  # Hardcoded to VENDOR
            "contactsEmail": row.get('Assessment POC', '').strip(),
            
            # Required boolean fields - set defaults
            "isSubProcessor": True,
            "isSubProcessorActive": True,
            "hasPii": self.detect_has_pii(row),  # Smart detection
            "passwordRequiresMinLength": False,
            "passwordRequiresNumber": False,
            "passwordRequiresSymbol": False,
            "passwordMfaEnabled": False,
        }
        
        # Only add optional fields if they have valid values
        url = self.parse_url(row)
        if url:
            vendor_data["url"] = url
            
        risk = self.parse_risk(row)
        if risk:
            vendor_data["risk"] = risk
            
        impact_level = self.parse_impact_level(row)
        if impact_level and impact_level != "UNSCORED":
            vendor_data["impactLevel"] = impact_level
            
        location = row.get('Location', '').strip()
        if location:
            vendor_data["location"] = location
            
        services = row.get('Description', '').strip()
        if services:
            vendor_data["servicesProvided"] = services
            
        notes = self.parse_notes(row)
        if notes:
            vendor_data["notes"] = notes
            
        # contact_email = self.parse_contact_email(row)
        # if contact_email:
        #     vendor_data["contactEmail"] = contact_email
        
        custom_fields = self.parse_custom_fields(row)
        if custom_fields:
            vendor_data["customFields"] = custom_fields
        
        # Remove any None values but keep boolean False values
        vendor_data = {k: v for k, v in vendor_data.items() if v is not None and v != ""}
            
        return vendor_data

    def import_vendors(self, csv_path: str) -> None:
        """Import vendors from CSV file to Drata"""
        success_count = 0
        error_count = 0
        skipped_count = 0
        error_logs_path = 'vendor_import_errors.csv'
        
        try:
            with open(csv_path, 'r', encoding='utf-8-sig') as file:  # utf-8-sig handles BOM
                reader = csv.DictReader(file)
                
                # Clean up column names to remove any BOM or extra whitespace
                if reader.fieldnames:
                    reader.fieldnames = [name.strip().lstrip('\ufeff') for name in reader.fieldnames]
                
                # Log available columns for debugging
                self.logger.info(f"CSV columns found: {reader.fieldnames}")
                
                # Check if Vendor column exists
                vendor_column = None
                for col in reader.fieldnames:
                    if col.lower().strip() in ['vendor', 'name']:
                        vendor_column = col
                        break
                
                if not vendor_column:
                    self.logger.error(f"No 'Vendor' or 'Name' column found in CSV. Available columns: {reader.fieldnames}")
                    return
                
                self.logger.info(f"Using '{vendor_column}' as vendor name column")
                
                for row_num, row in enumerate(reader, start=2):  # Start at 2 for header row
                    try:
                        # Skip empty rows
                        vendor_name = row.get(vendor_column, '').strip()
                        if not vendor_name:
                            skipped_count += 1
                            self.logger.debug(f"Row {row_num}: Skipping row with empty vendor name. Row data: {dict(row)}")
                            continue
                            
                        # Update row to use standard 'Vendor' key for consistency
                        if vendor_column != 'Vendor':
                            row['Vendor'] = vendor_name
                            
                        vendor_data = self.transform_vendor_data(row)
                        
                        self.logger.debug(f"Transformed data for {vendor_name}: {json.dumps(vendor_data, indent=2)}")
                        
                        # Check if vendor already exists
                        existing_vendor = self.find_vendor_by_name(vendor_name)
                        
                        if existing_vendor:
                            # Update existing vendor
                            vendor_id = existing_vendor.get('id')
                            success, status_code, response_text = self.update_vendor(vendor_id, vendor_data)
                            if success:
                                success_count += 1
                                self.logger.info(f"Successfully updated existing vendor: {vendor_name} (ID: {vendor_id})")
                            else:
                                error_count += 1
                                self.logger.error(f"Failed to update vendor {vendor_name} (ID: {vendor_id}) (row {row_num}): Status {status_code}, Response: {response_text}")
                                self.write_error_to_csv(error_logs_path, row, status_code, response_text, vendor_data, row_num)
                        else:
                            # Create new vendor
                            success, status_code, response_text = self.create_vendor(vendor_data)
                            if success:
                                success_count += 1
                                self.logger.info(f"Successfully created new vendor: {vendor_name}")
                            else:
                                error_count += 1
                                self.logger.error(f"Failed to create vendor {vendor_name} (row {row_num}): Status {status_code}, Response: {response_text}")
                                self.write_error_to_csv(error_logs_path, row, status_code, response_text, vendor_data, row_num)
                            
                    except Exception as e:
                        error_count += 1
                        traceback.print_exc()
                        self.logger.error(f"Error processing vendor {row.get(vendor_column, 'Unknown')} (row {row_num}): {str(e)}")
                        # Write exception details to CSV
                        self.write_error_to_csv(error_logs_path, row, 0, str(e), None, row_num)
                        
        except FileNotFoundError:
            self.logger.error(f"CSV file not found: {csv_path}")
        except Exception as e:
            self.logger.error(f"Error reading CSV file: {str(e)}")
            
        self.logger.info(f"Import completed. Successful: {success_count}, Failed: {error_count}, Skipped: {skipped_count}")

        if error_count > 0:
            self.logger.info(f"Error details written to {error_logs_path}")
        if skipped_count > 0:
            self.logger.info(f"Skipped {skipped_count} rows with empty vendor names")

    def write_error_to_csv(self, error_logs_path: str, row: Dict[str, Any], status_code: int, 
                          response_text: str, vendor_data: Dict[str, Any] = None, row_num: int = None):
        """Write error information to a CSV file"""
        # Create the file with headers if it doesn't exist
        file_exists = os.path.isfile(error_logs_path)
        
        try:
            # Parse the response text to extract error message
            error_message = "Unknown error"
            error_property = "N/A"
            try:
                response_json = json.loads(response_text)
                if 'message' in response_json:
                    error_message = response_json['message']
                if 'property' in response_json:
                    error_property = response_json['property']
            except:
                error_message = response_text[:200]  # Truncate long messages
            
            with open(error_logs_path, mode='a', newline='', encoding='utf-8') as file:
                fieldnames = ['Row Number', 'Vendor Name', 'Status Code', 'Error Property', 'Error Message', 'Timestamp', 'Vendor Data']
                writer = csv.DictWriter(file, fieldnames=fieldnames)
                
                if not file_exists:
                    writer.writeheader()
                
                writer.writerow({
                    'Row Number': row_num or 'Unknown',
                    'Vendor Name': row.get('Vendor', 'Unknown'),
                    'Status Code': status_code,
                    'Error Property': error_property,
                    'Error Message': error_message,
                    'Timestamp': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                    'Vendor Data': json.dumps(vendor_data) if vendor_data else 'N/A'
                })
                
            self.logger.info(f"Error details for vendor {row.get('Vendor', 'Unknown')} written to {error_logs_path}")
        except Exception as e:
            self.logger.error(f"Failed to write error log to CSV: {str(e)}")

    def find_vendor_by_name(self, vendor_name: str) -> Optional[Dict[str, Any]]:
        """Find a vendor by name using the Drata API"""
        try:
            # Query vendors by name
            response = requests.get(
                f"{self.base_url}/vendors",
                headers=self.headers,
                params={"q": vendor_name}
            )
            
            self.logger.debug(f"Vendor search API request: GET {self.base_url}/vendors?q={vendor_name}")
            self.logger.debug(f"Vendor search response status: {response.status_code}")
            self.logger.debug(f"Vendor search response body: {response.text}")
            
            if response.status_code == 200:
                data = response.json()
                vendors = data.get('data', [])
                
                # Look for exact match first
                for vendor in vendors:
                    if vendor.get('name', '').lower().strip() == vendor_name.lower().strip():
                        self.logger.info(f"Found existing vendor: {vendor_name} (ID: {vendor.get('id')})")
                        return vendor
                
                # If no exact match, return None
                if vendors:
                    self.logger.debug(f"Found {len(vendors)} vendors but no exact match for '{vendor_name}'")
                else:
                    self.logger.debug(f"No vendors found for search term '{vendor_name}'")
                return None
            else:
                self.logger.warning(f"Failed to search for vendor '{vendor_name}': Status {response.status_code}, Response: {response.text}")
                return None
                
        except Exception as e:
            self.logger.error(f"Error searching for vendor '{vendor_name}': {str(e)}")
            return None

    def update_vendor(self, vendor_id: int, vendor_data: Dict[str, Any]) -> Tuple[bool, int, str]:
        """Update an existing vendor"""
        try:
            self.logger.debug(f"Vendor update API request: PUT {self.base_url}/vendors/{vendor_id}")
            self.logger.debug(f"Vendor update request body: {json.dumps(vendor_data, indent=2)}")
            
            response = requests.put(
                f"{self.base_url}/vendors/{vendor_id}",
                headers=self.headers,
                json=vendor_data
            )
            
            self.logger.debug(f"Vendor update response status: {response.status_code}")
            self.logger.debug(f"Vendor update response body: {response.text}")
            
            return response.status_code == 200, response.status_code, response.text
            
        except Exception as e:
            self.logger.error(f"Error updating vendor ID {vendor_id}: {str(e)}")
            return False, 0, str(e)

    def create_vendor(self, vendor_data: Dict[str, Any]) -> Tuple[bool, int, str]:
        """Create a new vendor"""
        try:
            self.logger.debug(f"Vendor create API request: POST {self.base_url}/vendors")
            self.logger.debug(f"Vendor create request body: {json.dumps(vendor_data, indent=2)}")
            
            response = requests.post(
                f"{self.base_url}/vendors",
                headers=self.headers,
                json=vendor_data
            )
            
            self.logger.debug(f"Vendor create response status: {response.status_code}")
            self.logger.debug(f"Vendor create response body: {response.text}")
            
            return response.status_code == 201, response.status_code, response.text
            
        except Exception as e:
            self.logger.error(f"Error creating vendor: {str(e)}")
            return False, 0, str(e)

    def detect_has_pii(self, row: Dict[str, Any]) -> bool:
        """Detect if vendor likely handles PII based on description and notes"""
        text_to_check = " ".join([
            row.get('Description', ''),
            row.get('Notes', ''),
            row.get('Additional Comment(s)', '')
        ]).lower()
        
        # Keywords that suggest PII handling
        pii_keywords = [
            'patient', 'medical', 'health', 'personal', 'email', 'phone', 
            'address', 'ssn', 'social security', 'credit card', 'payment',
            'billing', 'customer data', 'user data', 'employee data'
        ]
        
        return any(keyword in text_to_check for keyword in pii_keywords)

    def parse_custom_fields(self, row: Dict[str, Any]) -> Optional[list]:
        """Parse custom fields from CSV columns"""
        custom_fields = []
        
        # Product custom field
        product = row.get("Product", "").strip()
        self.logger.debug(f"Product value from CSV: '{product}' for vendor {row.get('Vendor', 'Unknown')}")
        if product:
            custom_fields.append({
                "id": 1,
                "value": product
            })
            self.logger.debug(f"Added Product custom field: {product}")
        else:
            self.logger.debug(f"Product field is empty or missing for vendor {row.get('Vendor', 'Unknown')}")
        
        # Likelihood custom field
        likelihood = row.get("Likelihood", "").strip()
        self.logger.debug(f"Likelihood value from CSV: '{likelihood}' for vendor {row.get('Vendor', 'Unknown')}")
        if likelihood:
            custom_fields.append({
                "id": 2,
                "value": likelihood
            })
            self.logger.debug(f"Added Likelihood custom field: {likelihood}")
        else:
            self.logger.debug(f"Likelihood field is empty or missing for vendor {row.get('Vendor', 'Unknown')}")
        
        self.logger.debug(f"Final custom_fields array: {custom_fields}")
        return custom_fields if custom_fields else None

if __name__ == "__main__":
    import argparse
    
    parser = argparse.ArgumentParser(description='Import vendors from CSV to Drata')
    parser.add_argument('csv_file', help='Path to the CSV file containing vendor data (default: vendors.csv)', 
                       nargs='?', default='vendors.csv')
    parser.add_argument('api_key', help='Drata API key')
    parser.add_argument('--base-url', default='https://public-api.drata.com/public/v2', help='Drata API base URL')
    parser.add_argument('--debug', action='store_true', help='Enable debug logging')
    
    args = parser.parse_args()
    
    # Create importer first
    importer = DrataVendorImporter(args.api_key, args.base_url)
    
    # Then set debug level if requested
    if args.debug:
        logging.getLogger().setLevel(logging.DEBUG)
        importer.logger.setLevel(logging.DEBUG)
        importer.logger.info("Debug logging enabled")
    
    importer.import_vendors(args.csv_file)

