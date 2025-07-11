# aws-ec2-ami-builder
### Key Points:

1. *Automatic File Type Detection*:
   - Detects both package lists and executable scripts
   - Uses heuristics:
     - If file starts with shebang (e.g., #!/bin/bash)
     - If file has .sh extension
     - If file contains only valid package names (alphanumeric + special chars -_.+:@)
     - Otherwise treats as script

2. *Flexible Source Handling*:
   - Processes both package lists and scripts from multiple buckets
   - Combines all packages into single install command
   - Executes scripts in the order they're specified

3. *Enhanced Command Generation*:
   python
   # Example command sequence:
   commands = [
        "sudo yum update -y",
        "sudo yum install -y pkg1 pkg2 pkg3",  # Combined from package files
        "script1_content",  # Executed as bash script
        "script2_content",  # Executed as bash script
        "sudo yum clean all",
        "sudo rm -rf /var/cache/yum"
   ]
   

4. *Improved Content Analysis*:
   - Checks first 100 characters for shebang pattern
   - Validates package name format (alphanumeric + -_.+:@)
   - Handles comments in package files

5. *Better Logging*:
   - Logs each downloaded file
   - Logs detected file type
   - Shows truncated commands for readability
   - Provides detailed error messages

### Example File Types:

1. *Package List* (packages.txt):
   
   # Base packages
   httpd
   php8.2
   mysql-client
   
   # Security tools
   fail2ban
   clamav
   

2. *Bash Script* (setup.sh):
   bash
   #!/bin/bash
   # Configure web server
   echo "ServerName localhost" | sudo tee -a /etc/httpd/conf/httpd.conf
   sudo systemctl enable httpd
   
   # Install custom app
   curl -sSL https://example.com/installer | sudo bash
   

### Usage Instructions:

1. *Set Environment Variables*:
   bash
   export PACKAGE_SOURCES='["bucket1:packages.txt", "bucket2:setup.sh"]'
   export PARAM_STORE_NAME="/prod/ami/latest"
   export INSTANCE_PROFILE_NAME="EC2-SSM-Access"
   

2. *Execution*:
   bash
   python create_custom_ami.py
   

### Processing Workflow:

1. *Download and Analyze*:
   - Download each file from S3
   - Determine if it's a package list or script
   - Package lists: Extract package names
   - Scripts: Keep as executable content

2. *Command Generation*:
   - Create combined package install command
   - Maintain script execution commands
   - Add system update and cleanup commands

3. *Execution Order*:
   
   1. System update
   2. Package installation (all packages combined)
   3. Scripts (in order specified)
   4. System cleanup
   

### Error Handling:
- Skips invalid sources
- Fails on large errors
- Provides detailed error messages
- Ensures instance termination even on failures
- Handles SSM command timeouts (30 minutes)

This solution provides maximum flexibility, allowing you to mix package lists and executable scripts from multiple S3 buckets while maintaining a simple configuration interface. The automatic detection ensures you don't need to specify file types manually.
