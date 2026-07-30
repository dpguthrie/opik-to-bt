variable "name" {
  description = "Name prefix for the migration runner."
  type        = string
  default     = "opik-to-bt"
}

variable "instance_type" {
  description = "EC2 instance type."
  type        = string
  default     = "m7g.xlarge"
}

variable "root_volume_gb" {
  description = "Encrypted gp3 space for the checkout, rolling partitions, and checkpoints."
  type        = number
  default     = 250
}

variable "repo_url" {
  description = "Git URL for this repository."
  type        = string
}

variable "repo_ref" {
  description = "Git branch or tag to check out."
  type        = string
  default     = "main"
}
