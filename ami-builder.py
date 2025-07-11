import boto3
import time
import os
import json
import tempfile
import logging
import re
import subprocess
from botocore.exceptions import ClientError

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger()

def get_latest_amazon_linux_ami():
    """Fetch latest Amazon Linux 2 AMI ID"""
    ec2 = boto3.client('ec2')
    try:
        response = ec2.describe_images(
            Owners=['amazon'],
            Filters=[
                {'Name': 'name', 'Values': ['amzn2-ami-kernel-*-hvm-*-gp2']},
                {'Name': 'architecture', 'Values': ['x86_64']},
                {'Name': 'state', 'Values': ['available']}
            ]
        )
        images = sorted(response['Images'], key=lambda x: x['CreationDate'], reverse=True)
        return images[0]['ImageId']
    except ClientError as e:
        logger.error(f"Failed to get AMI: {e}")
        raise

def download_s3_file(bucket, key, local_path):
    """Download file from S3 with error handling"""
    s3 = boto3.client('s3')
    try:
        s3.download_file(bucket, key, local_path)
        logger.info(f"Downloaded s3://{bucket}/{key}")
        return True
    except ClientError as e:
        logger.error(f"Failed to download s3://{bucket}/{key}: {e}")
        return False

def process_file_content(file_path):
    """Determine file type and return appropriate commands"""
    with open(file_path, 'r') as f:
        content = f.read()
    
    # Check if file appears to be a script
    if re.search(r'^#!.*\b(bash|sh)\b', content[:100]) or file_path.endswith('.sh'):
        logger.info(f"Detected script file: {file_path}")
        return {
            'type': 'script',
            'content': content
        }
    
    # Check if file contains only valid package names
    package_lines = []
    for line in content.splitlines():
        line = line.strip()
        if line and not line.startswith('#'):
            if re.match(r'^[a-zA-Z0-9_\-\+\.:@]+$', line):
                package_lines.append(line)
            else:
                logger.info(f"Non-package content detected: {line}")
                return {
                    'type': 'script',
                    'content': content
                }
    
    logger.info(f"Detected package list: {file_path}")
    return {
        'type': 'packages',
        'content': " ".join(package_lines)
    }

def process_sources(package_sources):
    """Process all sources and return commands"""
    s3 = boto3.client('s3')
    commands = []
    package_list = []
    temp_dir = tempfile.mkdtemp()
    
    for source in package_sources:
        try:
            # Parse source format: [type:]bucket:key
            parts = source.split(':', 1)
            if len(parts) < 2:
                logger.warning(f"Invalid source format: {source}")
                continue
                
            bucket = parts[0]
            key = parts[1]
            local_path = os.path.join(temp_dir, os.path.basename(key))
            
            if not download_s3_file(bucket, key, local_path):
                continue
                
            # Analyze file content
            file_info = process_file_content(local_path)
            
            if file_info['type'] == 'packages':
                package_list.append(file_info['content'])
            elif file_info['type'] == 'script':
                commands.append(file_info['content'])
                
        except Exception as e:
            logger.error(f"Error processing {source}: {e}")
    
    # Combine all packages into one install command
    if package_list:
        combined_packages = " ".join(package_list)
        commands.insert(0, f"sudo yum install -y {combined_packages}")
    
    return commands

def create_custom_ami():
    # Initialize clients
    ec2 = boto3.client('ec2')
    ssm = boto3.client('ssm')
    
    # Configuration
    try:
        package_sources = json.loads(os.environ['PACKAGE_SOURCES'])
        param_name = os.environ['PARAM_STORE_NAME']
        instance_type = os.environ.get('INSTANCE_TYPE', 't2.micro')
        key_name = os.environ.get('KEY_NAME', '')
        instance_profile_name = os.environ.get('INSTANCE_PROFILE_NAME', '')
        
        if not package_sources:
            raise ValueError("PACKAGE_SOURCES is empty")
    except KeyError as e:
        logger.error(f"Missing environment variable: {e}")
        return
    except json.JSONDecodeError:
        logger.error("Invalid JSON in PACKAGE_SOURCES")
        return

    instance_id = None
    ami_id = None

    try:
        # 1. Get latest Amazon Linux AMI
        base_ami = get_latest_amazon_linux_ami()
        logger.info(f"Using base AMI: {base_ami}")

        # 2. Process all sources
        commands = process_sources(package_sources)
        if not commands:
            raise RuntimeError("No valid commands generated from sources")
        
        # Prepend system updates
        commands.insert(0, "sudo yum update -y")
        
        # Append cleanup commands
        commands.extend([
            "sudo yum clean all",
            "sudo rm -rf /var/cache/yum"
        ])
        
        logger.info("Generated commands:")
        for i, cmd in enumerate(commands, 1):
            logger.info(f"{i}. {cmd[:80]}{'...' if len(cmd) > 80 else ''}")

        # 3. Create EC2 instance
        launch_config = {
            'ImageId': base_ami,
            'InstanceType': instance_type,
            'MinCount': 1,
            'MaxCount': 1,
            'TagSpecifications': [{
                'ResourceType': 'instance',
                'Tags': [{'Key': 'Name', 'Value': 'Custom-AMI-Builder'}]
            }]
        }
        
        if instance_profile_name:
            launch_config['IamInstanceProfile'] = {'Name': instance_profile_name}
            logger.info(f"Using IAM instance profile: {instance_profile_name}")
        else:
            logger.warning("No IAM instance profile specified - ensure instance has S3/SSM access")
        
        if key_name:
            launch_config['KeyName'] = key_name

        response = ec2.run_instances(**launch_config)
        instance_id = response['Instances'][0]['InstanceId']
        logger.info(f"Launched instance: {instance_id}")

        # 4. Wait for running state
        ec2.get_waiter('instance_running').wait(InstanceIds=[instance_id])
        logger.info("Instance is running")

        # 5. Execute commands via SSM
        command_id = ssm.send_command(
            InstanceIds=[instance_id],
            DocumentName='AWS-RunShellScript',
            Parameters={'commands': commands},
            TimeoutSeconds=1800  # 30 minutes timeout
        )['Command']['CommandId']
        
        # Wait for command completion
        for _ in range(60):  # 30 minute timeout (60*30s)
            result = ssm.get_command_invocation(
                CommandId=command_id,
                InstanceId=instance_id
            )
            status = result['Status']
            if status in ['Success', 'Failed', 'Cancelled', 'TimedOut']:
                if status != 'Success':
                    error = result.get('StandardErrorContent', 'No error details')
                    raise RuntimeError(f"Command failed: {status} - {error}")
                logger.info("All commands executed successfully")
                break
            time.sleep(30)
        else:
            raise RuntimeError("Command execution timed out")

        # 6. Create AMI
        ami_name = f"custom-ami-{int(time.time())}"
        ami_response = ec2.create_image(
            InstanceId=instance_id,
            Name=ami_name,
            Description=f"Custom AMI with packages/scripts from {len(package_sources)} sources",
            NoReboot=True
        )
        ami_id = ami_response['ImageId']
        logger.info(f"Creating AMI: {ami_id}")

        ec2.get_waiter('image_available').wait(ImageIds=[ami_id])
        logger.info("AMI is now available")

        # 7. Store AMI ID in Parameter Store
        ssm.put_parameter(
            Name=param_name,
            Value=ami_id,
            Type='String',
            Overwrite=True
        )
        logger.info(f"Stored AMI ID in Parameter Store: {param_name} = {ami_id}")

    except Exception as e:
        logger.error(f"Process failed: {str(e)}")
        raise
    finally:
        if instance_id:
            try:
                ec2.terminate_instances(InstanceIds=[instance_id])
                logger.info(f"Terminated instance: {instance_id}")
            except ClientError as e:
                logger.error(f"Failed to terminate instance: {e}")

if __name__ == "__main__":
    required_vars = ['PACKAGE_SOURCES', 'PARAM_STORE_NAME']
    
    if all(var in os.environ for var in required_vars):
        try:
            create_custom_ami()
        except Exception:
            logger.error("AMI creation process failed")
            exit(1)
    else:
        missing = [var for var in required_vars if var not in os.environ]
        logger.error(f"Missing environment variables: {', '.join(missing)}")
        exit(1)
