# Use the lightweight Nginx Alpine image
FROM nginx:alpine

# Copy the SPA into the Nginx default serving directory
COPY index.html /usr/share/nginx/html/index.html

# Expose port 80 so Cloud Run knows where to send traffic
EXPOSE 80