FROM node:20-slim

WORKDIR /app

# Copy package.json from the cloudrun directory
COPY cloudrun/package.json ./

# Install production dependencies
RUN npm install --omit=dev

# Copy the server code
COPY cloudrun/server.js ./

# Expose the standard Cloud Run port
EXPOSE 8080

# Run the server
CMD ["node", "server.js"]
